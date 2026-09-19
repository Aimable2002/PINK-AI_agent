"""
The two Celery tasks that make the signal pipeline resilient rather than
a fragile inline call chain inside the daemon's event handler:

  score_signal_job   -- runs the LLM confidence-scoring step for one
                         message that already passed the daemon's cheap
                         Layer-1 filter. Queued from the daemon so a slow
                         or failing scoring call never blocks the live
                         Telegram event loop for every other user.

  send_telegram_alert_job -- delivers the alert with real retry/backoff,
                              same pattern as run_paid_job/run_free_job.
                              Kept as its own task (not inlined into
                              scoring) so "did the AI finish scoring" and
                              "did the message actually arrive" are
                              distinct, separately-retryable, separately
                              -loggable failure modes.

Both run on the existing free_standard/paid_priority queues -- no new
Celery infrastructure, just new task names.
"""

from __future__ import annotations

import asyncio

from app.agent_services import REGISTRY
from app.agent_services.base import AgentServicePaused
from app.connectors.manager import MCPConnectorManager
from app.connectors import telegram_service
from app.data.supabase_client import insert_signal, update_signal
from app.queue.celery_app import celery_app


@celery_app.task(name="app.queue.agent_tasks.score_signal_job", bind=True, max_retries=2)
def score_signal_job(self, user_id: str, service_row: dict, channel: str | None, raw_text: str):
    module = REGISTRY.get(service_row.get("service_id"))
    if module is None:
        return {"status": "error", "detail": f"unknown service_id '{service_row.get('service_id')}'"}

    # Inserted once, outside the retried block below -- Celery re-runs this
    # whole function on every retry, and inserting here used to mean a
    # failed-then-retried attempt left a fresh duplicate row per retry for
    # the same incoming message.
    signal_row = insert_signal(user_id, service_row["id"], channel, raw_text)

    try:
        outcome = asyncio.run(module.score_signal(user_id, service_row, channel, raw_text, signal_row))
    except AgentServicePaused as exc:
        return {"status": "paused", "reason": exc.reason}
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            update_signal(signal_row["id"], alert_error=str(exc))
            return {"status": "error", "detail": str(exc)}
        raise self.retry(exc=exc, countdown=5)

    if outcome.get("should_alert"):
        alert_text = module.format_alert(outcome["channel"], outcome["raw_text"], outcome["confidence"])
        send_telegram_alert_job.delay(
            user_id, outcome["signal_id"], outcome["alert_chat"], alert_text
        )

    return {"status": "scored", **{k: v for k, v in outcome.items() if k != "raw_text"}}


@celery_app.task(name="app.queue.agent_tasks.generate_signal_job", bind=True, max_retries=2)
def generate_signal_job(self, user_id: str, service_row: dict):
    module = REGISTRY.get(service_row.get("service_id"))
    if module is None:
        return {"status": "error", "detail": f"unknown service_id '{service_row.get('service_id')}'"}

    try:
        manager = MCPConnectorManager()
        asyncio.run(manager.load_user_connectors(user_id))
        outcome = asyncio.run(module.generate_signal(user_id, service_row, manager))
    except AgentServicePaused as exc:
        return {"status": "paused", "reason": exc.reason}
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            return {"status": "error", "detail": str(exc)}
        raise self.retry(exc=exc, countdown=5)

    return {"status": "generated", **outcome}


@celery_app.task(name="app.queue.agent_tasks.send_telegram_alert_job", bind=True, max_retries=3)
def send_telegram_alert_job(self, user_id: str, signal_id: str, chat: str, text: str):
    try:
        asyncio.run(telegram_service.send_message(user_id, chat, text))
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            update_signal(signal_id, alerted=False, alert_error=str(exc))
            return {"status": "failed", "detail": str(exc)}
        raise self.retry(exc=exc, countdown=10)

    update_signal(signal_id, alerted=True)
    return {"status": "sent"}