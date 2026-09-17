"""
Agent services listener daemon.

This is the one genuinely new piece of infrastructure the whole agent-
services product needed: a long-running process holding a live Telegram
connection per user with an active service, reacting to messages the
instant they arrive. Nothing else in the codebase does this -- every
other Telegram function (send_message, list_chats) opens a connection,
does one thing, and closes it.

Process model: single process, many concurrent sessions (confirmed
choice, given current VPS headroom -- revisit only if/when real
multi-tenant scale makes memory-per-process a real constraint; see
app/connectors/telegram_service.py's own docstring for the same tradeoff
noted at the session-manager level).

Run under pm2 as its own process, separate from pink-api and the two
celery workers -- it is not an HTTP server and not a celery worker, it's
a third kind of long-running process this app now has:

    pm2 start /data/pink/PINK-AI_agent/.venv/bin/python \\
      --name pink-agents-daemon \\
      --interpreter none \\
      -- /data/pink/PINK-AI_agent/app/agents_daemon.py

(ecosystem.config.js on the VPS isn't in this repo -- add the
equivalent block there instead of relying on this command being run by
hand, per the deploy-hygiene lessons from earlier.)
"""

from __future__ import annotations

import asyncio
import logging

from telethon import events

from app.agent_services.telegram_signal_monitor import SERVICE_ID, looks_like_trading_text
from app.connectors.telegram_service import close_persistent_client, open_persistent_client
from app.data.supabase_client import get_active_services, upsert_telegram_session_row
from app.queue.agent_tasks import score_signal_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agents_daemon")

RECONCILE_INTERVAL_SECONDS = 60

# user_id -> {"client": TelegramClient, "service_row": dict, "handler": callable}
_live: dict[str, dict] = {}


def _make_handler(user_id: str, service_row: dict):
    async def handler(event) -> None:
        try:
            raw_text = event.raw_text or ""
            if not looks_like_trading_text(raw_text):
                return

            chat = await event.get_chat()
            channel_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)

            score_signal_job.delay(user_id, service_row, channel_name, raw_text)
            log.info("queued scoring for user=%s chat=%s", user_id, channel_name)
        except Exception:
            log.exception("event handler failed for user=%s", user_id)

    return handler


async def _connect_user(service_row: dict) -> None:
    user_id = service_row["user_id"]
    monitored_chats = (service_row.get("config") or {}).get("monitored_chats") or []
    if not monitored_chats:
        return

    try:
        client = await open_persistent_client(user_id)
    except Exception as exc:
        log.warning("could not open Telegram session for user=%s: %s", user_id, exc)
        return

    # Telethon only skips entity resolution for real `int`s (a marked/negative
    # ID is used as-is). Anything else -- including these numeric strings, as
    # stored by the frontend -- goes through get_input_entity(), which needs
    # an access_hash from this connection's own entity cache. That's empty on
    # a fresh daemon connection, so a bare ID string can't resolve and raises
    # ValueError. Casting to int avoids needing that lookup at all.
    try:
        chat_ids = [int(c) for c in monitored_chats]
    except (TypeError, ValueError):
        log.warning("user=%s has a non-numeric monitored chat id in %r, skipping", user_id, monitored_chats)
        await close_persistent_client(client)
        return

    handler = _make_handler(user_id, service_row)
    client.add_event_handler(handler, events.NewMessage(chats=chat_ids))
    _live[user_id] = {"client": client, "service_row": service_row, "handler": handler}
    log.info("listening for user=%s on %d chat(s)", user_id, len(chat_ids))


async def _disconnect_user(user_id: str) -> None:
    entry = _live.pop(user_id, None)
    if entry:
        await close_persistent_client(entry["client"])
        log.info("stopped listening for user=%s", user_id)


async def _reconcile() -> None:
    """
    Diffs desired state (active rows in user_agent_services) against
    live connections, on a timer -- this daemon doesn't restart when a
    user turns their service on/off or edits monitored_chats, so it has
    to notice the change itself.
    """
    active_rows = get_active_services(SERVICE_ID)
    desired = {row["user_id"]: row for row in active_rows}

    for user_id, row in desired.items():
        existing = _live.get(user_id)
        if existing is None or existing["service_row"].get("config") != row.get("config"):
            if existing is not None:
                await _disconnect_user(user_id)
            await _connect_user(row)

    # No longer active -- disconnect.
    for user_id in list(_live.keys()):
        if user_id not in desired:
            await _disconnect_user(user_id)


async def _liveness_check() -> None:
    """
    Sessions can die silently (per the roadmap's own warning, and per
    _ShortLivedClient's identical check on the on-demand path). Without
    this, a dead listener looks identical to a healthy one that simply
    has nothing to report -- the worst kind of silent failure for an
    always-on product. This doesn't attempt reconnection itself (Telethon
    clients generally reconnect their own transport); it specifically
    catches the "still connected but no longer authorized" case and
    marks it in telegram_sessions so the UI can tell the user to log in
    again, instead of the service quietly doing nothing forever.
    """
    for user_id, entry in list(_live.items()):
        client = entry["client"]
        try:
            if not client.is_connected() or not await client.is_user_authorized():
                raise RuntimeError("session no longer authorized")
        except Exception:
            log.warning("session for user=%s appears dead; marking disconnected", user_id)
            await upsert_telegram_session_row(
                user_id, status="disconnected", last_error="Listener detected a dead session; reconnect required."
            )
            await _disconnect_user(user_id)


async def main() -> None:
    log.info("agent services daemon starting (reconcile every %ss)", RECONCILE_INTERVAL_SECONDS)
    while True:
        try:
            await _reconcile()
            await _liveness_check()
        except Exception:
            log.exception("reconcile/liveness pass failed")
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())