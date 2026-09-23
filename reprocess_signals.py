"""
Reprocess existing pending Telegram signals in place.

Dry run:
    .venv/bin/python reprocess_signals.py

Reprocess one signal without sending an alert:
    .venv/bin/python reprocess_signals.py --confirm --limit 1

Reprocess selected rows and send alerts for successfully parsed signals:
    .venv/bin/python reprocess_signals.py --confirm --send-alerts --signal-id SIGNAL_ID

This script does not create duplicate signals. It calls the same scoring
function as the Celery task, but keeps the existing signal row and leaves
historical alerts disabled by default.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.agent_services import REGISTRY
from app.agent_services.base import AgentServicePaused
from app.connectors import telegram_service
from app.data.supabase_client import get_client, update_signal

PAGE_SIZE = 200


def fetch_pending_signals(client, signal_id: str | None, limit: int) -> list[dict]:
    query = (
        client.table("signals")
        .select("id, user_id, agent_service_id, channel, raw_text, parse_status, created_at")
        .eq("parse_status", "pending")
        .order("created_at", desc=False)
        .limit(limit)
    )
    if signal_id:
        query = query.eq("id", signal_id)
    return query.execute().data or []


def fetch_service(client, service_id: str) -> dict | None:
    response = client.table("user_agent_services").select("*").eq("id", service_id).limit(1).execute()
    return response.data[0] if response.data else None


async def reprocess_one(row: dict, service_row: dict, send_alerts: bool) -> str:
    module = REGISTRY.get(service_row.get("service_id"))
    if module is None:
        raise RuntimeError(f"unknown service_id {service_row.get('service_id')!r}")

    # This is a background signal run, so it has no row in the chat `tasks`
    # table and must not pass a Celery id as the usage_events foreign key.
    service_row = {**service_row, "_billing_task_id": None}
    outcome = await module.score_signal(
        row["user_id"],
        service_row,
        row.get("channel"),
        row.get("raw_text") or "",
        row,
    )

    if send_alerts and outcome.get("should_alert"):
        alert_text = module.format_alert(outcome["channel"], {
            key: value for key, value in outcome.items()
            if key in {"signal_type", "symbol", "direction", "order_type", "entry", "take_profits", "stop_loss", "expiry_minutes"}
        })
        await telegram_service.send_message(row["user_id"], outcome["alert_chat"], alert_text)
        update_signal(row["id"], alerted=True)

    return outcome.get("parse_status", "unknown")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reprocess pending signals in place.")
    parser.add_argument("--confirm", action="store_true", help="actually reprocess rows")
    parser.add_argument("--limit", type=int, default=1, help="maximum rows to process (default: 1)")
    parser.add_argument("--signal-id", help="reprocess one specific signal row")
    parser.add_argument("--send-alerts", action="store_true", help="send Telegram alerts for parsed historical signals")
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")

    client = get_client()
    rows = fetch_pending_signals(client, args.signal_id, args.limit)
    print(f"found {len(rows)} pending signal(s)")

    for row in rows:
        preview = (row.get("raw_text") or "").replace("\n", " ")[:100]
        print(f"  {row['id']} [{row.get('created_at')}] {preview}")

    if not rows:
        return 0
    if not args.confirm:
        print("DRY RUN -- nothing changed. Re-run with --confirm to process these rows.")
        return 0

    succeeded = 0
    for row in rows:
        service_row = fetch_service(client, row["agent_service_id"])
        try:
            if not service_row:
                raise RuntimeError(f"agent service {row['agent_service_id']} was not found")
            status = asyncio.run(reprocess_one(row, service_row, args.send_alerts))
            print(f"reprocessed {row['id']}: {status}")
            succeeded += 1
        except AgentServicePaused as exc:
            update_signal(row["id"], alert_error=f"reprocess skipped: {exc.reason}")
            print(f"skipped {row['id']}: {exc.reason}")
        except Exception as exc:
            update_signal(row["id"], alert_error=f"reprocess failed: {exc}")
            print(f"failed {row['id']}: {exc}")

    print(f"completed {succeeded}/{len(rows)} signal(s)")
    return 0 if succeeded == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
