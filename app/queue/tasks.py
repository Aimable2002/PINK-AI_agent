import asyncio

from app.queue.celery_app import celery_app
from app.core.agent_runtime import run_agent_loop
from app.connectors.manager import MCPConnectorManager


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
        return _run(prompt, messages, connectors, mode, user_id)
    except Exception as exc:
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
        return _run(prompt, messages, connectors, mode, user_id)
    except Exception as exc:
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