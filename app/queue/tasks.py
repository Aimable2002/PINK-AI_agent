import asyncio
from datetime import datetime, timezone

from app.queue.celery_app import celery_app
from app.core.agent_runtime import run_agent_loop
from app.connectors.manager import MCPConnectorManager
from app.data.supabase_client import (
    get_task_by_job_id,
    get_user_context,
    record_usage,
    update_task_by_job_id,
)


async def get_remaining_credits(user_id: str) -> float | None:
    """Shared by chat jobs and agent-service tasks -- both need the same
    'how much balance is left right now' check before spending any of it."""
    try:
        user = await get_user_context(user_id)
        return max(0.0, float(user.quota_limit - user.quota_used))
    except Exception:
        return None


def charge_usage(user_id: str, result: dict, task_id: str | None = None) -> None:
    """
    The one place that turns an AgentRunResult into a real charge against
    a user's balance. Used by both chat jobs (_finish, below) and agent-
    service tasks (app/queue/agent_tasks.py) so a signal-monitor run is
    metered by the exact same rule as a chat run -- no second, looser
    accounting path for background-triggered work.
    """
    tiers = result.get("tiers_used") or ["small"]
    events = result.get("usage_events") or []
    if not events:
        events = [{
            "operation_type": "run",
            "provider": "backend",
            "resource": "unknown",
            "cost_usd": result.get("cost_usd", 0.0),
            "credits": result.get("credits_used", 0.0),
            "metadata": {},
        }]
    credits = sum(float(event.get("credits", 0.0) or 0.0) for event in events)
    cost_usd = sum(float(event.get("cost_usd", 0.0) or 0.0) for event in events)
    record_usage(
        user_id,
        tiers[-1],
        task_id=task_id,
        requests=credits,
        tool_calls=sum(
            step.get("connector") != "agent"
            for step in result.get("steps", [])
        ),
        cost_usd=cost_usd,
        usage_events=events,
    )


def _run(
    prompt: str,
    messages: list[dict],
    connectors: list[str] | None = None,
    mode: str = "chat",
    user_id: str | None = None,
) -> dict:
    async def _go() -> dict:
        manager = MCPConnectorManager()
        credit_budget = None
        if user_id:
            await manager.load_user_connectors(user_id)
            # Remaining balance, not the plan's total -- this is what
            # actually gets enforced mid-run in run_agent_loop. Fetched
            # once per job, not per iteration, to avoid a Supabase round
            # trip on every loop step.
            credit_budget = await get_remaining_credits(user_id)
        return await run_agent_loop(
            prompt,
            messages,
            connectors or [],
            connector_manager=manager,
            mode=mode,
            user_id=user_id,
            credit_budget=credit_budget,
        )

    return asyncio.run(_go())


def _cancelled(job_id: str) -> bool:
    task = get_task_by_job_id(job_id)
    return bool(task and task.get("status") == "cancelled")


def _finish(job_id: str, user_id: str, result: dict) -> dict:
    if _cancelled(job_id):
        update_task_by_job_id(
            job_id,
            status="cancelled",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"status": "cancelled"}

    failed = result.get("stopped_reason") == "llm_call_failed"
    update_task_by_job_id(
        job_id,
        status="failed" if failed else "completed",
        progress=100,
        output=result.get("final_message") if not failed else None,
        error=result.get("final_message") if failed else None,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    if user_id:
        task = get_task_by_job_id(job_id)
        # This is the actual fix to the flat-rate quota bug: `requests`
        # used to always be 1 regardless of tier/tool calls/tokens. It
        # now carries the real measured credit cost of this specific run
        # (see agent_runtime.AgentRunResult + llm_client.extract_usage),
        # so a 10-iteration multi-connector run costs proportionally more
        # than a one-line reply, instead of costing the same "1".
        charge_usage(user_id, result, task_id=task.get("id") if task else None)
    return result


@celery_app.task(name="app.queue.tasks.run_paid_job", bind=True, max_retries=2)
def run_paid_job(
    self,
    prompt: str,
    messages: list[dict],
    connectors: list[str] | None = None,
    mode: str = "chat",
    user_id: str | None = None,
):
    try:
        if _cancelled(self.request.id):
            return {"status": "cancelled"}
        return _finish(self.request.id, user_id, _run(prompt, messages, connectors, mode, user_id))
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            update_task_by_job_id(
                self.request.id,
                status="failed",
                progress=100,
                error=str(exc),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        raise self.retry(exc=exc, countdown=2)


@celery_app.task(name="app.queue.tasks.run_free_job", bind=True, max_retries=1)
def run_free_job(
    self,
    prompt: str,
    messages: list[dict],
    connectors: list[str] | None = None,
    mode: str = "chat",
    user_id: str | None = None,
):
    try:
        if _cancelled(self.request.id):
            return {"status": "cancelled"}
        return _finish(self.request.id, user_id, _run(prompt, messages, connectors, mode, user_id))
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            update_task_by_job_id(
                self.request.id,
                status="failed",
                progress=100,
                error=str(exc),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        raise self.retry(exc=exc, countdown=5)


@celery_app.task(name="app.queue.tasks.health_check_job")
def health_check_job(x: int, y: int) -> int:
    """
    Infra-only task with no LLM/MCP dependency. Used to prove the real
    worker execution path (Redis broker -> worker process -> result
    backend) end-to-end, independent of model provider credentials this
    environment doesn't have.
    """
    return x + y