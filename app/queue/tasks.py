import asyncio
from datetime import datetime, timezone

from app.queue.celery_app import celery_app
from app.core.agent_runtime import run_agent_loop
from app.connectors.manager import MCPConnectorManager
from app.data.supabase_client import (
    get_task_by_job_id,
    record_usage,
    update_task_by_job_id,
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
        if user_id:
            await manager.load_user_connectors(user_id)
        return await run_agent_loop(
            prompt,
            messages,
            connectors or [],
            connector_manager=manager,
            mode=mode,
            user_id=user_id,
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
        tiers = result.get("tiers_used") or ["small"]
        task = get_task_by_job_id(job_id)
        record_usage(
            user_id,
            tiers[-1],
            task_id=task.get("id") if task else None,
            tool_calls=sum(
                step.get("connector") != "agent"
                for step in result.get("steps", [])
            ),
        )
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
            if user_id:
                task = get_task_by_job_id(self.request.id)
                record_usage(user_id, "small", task_id=task.get("id") if task else None)
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
            if user_id:
                task = get_task_by_job_id(self.request.id)
                record_usage(user_id, "small", task_id=task.get("id") if task else None)
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