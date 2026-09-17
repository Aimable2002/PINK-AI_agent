"""
Telegram is not an MCP server -- there is no hosted endpoint to point a
url/transport/token config at. A Telegram connection is a live MTProto
session belonging to one personal account, established interactively
(phone -> OTP code -> optional 2FA password) via Telethon.

This module owns that whole lifecycle:
  - the interactive login flow (start / verify / 2fa)
  - encrypting the resulting session string at rest (a Telethon session
    string is equivalent to that user's live login -- anyone who has it
    can act as that account, so it is never stored in plaintext)
  - loading a saved session back into a live client to send a message or
    list chats, on demand, per call

Process-model note: login is interactive and short-lived, so it runs
in-process in the FastAPI web server (an in-memory dict of in-flight
Telethon clients keyed by user_id, cleared on completion/failure/expiry).
This is fine for a single FastAPI process. At real multi-instance scale,
the in-flight login state would need to move to Redis (a session can't be
split across two different processes mid-login) -- flagged here rather
than silently assumed away.

Completed sessions are NOT kept alive in memory between agent tool calls:
call_tool-style functions here open a short-lived client from the stored
encrypted session, do the one thing requested, and disconnect. This keeps
the "many concurrent authenticated clients" problem from your own roadmap
doc bounded -- there is no long-running per-user daemon in this version.
A liveness/health-check daemon (per your roadmap's item 8) is a follow-up,
not required for send/list to work.
"""

from __future__ import annotations

import time

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from app.config import TELEGRAM_API_HASH, TELEGRAM_API_ID, TELEGRAM_SESSION_ENCRYPTION_KEY
from app.data.supabase_client import (
    delete_telegram_session_row,
    get_telegram_session_row,
    upsert_telegram_session_row,
)

_LOGIN_TTL_SECONDS = 10 * 60  # abandon an in-flight login after 10 minutes

# user_id -> {"client": TelegramClient, "phone": str, "started_at": float}
_pending_logins: dict[str, dict] = {}


class TelegramConfigError(RuntimeError):
    """Raised when the app-level Telegram credentials aren't configured."""


class TelegramLoginError(RuntimeError):
    """Raised for any user-facing failure during the login flow."""


def _require_app_credentials() -> None:
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise TelegramConfigError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH are not configured. "
            "Get a single app-level pair from https://my.telegram.org."
        )
    if not TELEGRAM_SESSION_ENCRYPTION_KEY:
        raise TelegramConfigError(
            "TELEGRAM_SESSION_ENCRYPTION_KEY is not configured. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and never rotate it without "
            "expecting every stored session to need re-login."
        )


def _fernet() -> Fernet:
    return Fernet(TELEGRAM_SESSION_ENCRYPTION_KEY.encode())


def _encrypt_session(session_string: str) -> str:
    return _fernet().encrypt(session_string.encode()).decode()


def _decrypt_session(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise TelegramLoginError(
            "Stored Telegram session could not be decrypted (key rotated or "
            "corrupted row). The user needs to reconnect Telegram."
        ) from exc


def _drop_expired(user_id: str) -> None:
    entry = _pending_logins.get(user_id)
    if entry and time.monotonic() - entry["started_at"] > _LOGIN_TTL_SECONDS:
        _pending_logins.pop(user_id, None)


async def start_login(user_id: str, phone: str) -> dict:
    """Step 1: send the OTP code to `phone`. Returns {"step": "code"}."""
    _require_app_credentials()
    _drop_expired(user_id)

    if user_id in _pending_logins:
        old = _pending_logins.pop(user_id)
        try:
            await old["client"].disconnect()
        except Exception:
            pass

    client = TelegramClient(StringSession(), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.connect()
    try:
        await client.send_code_request(phone)
    except Exception as exc:
        await client.disconnect()
        raise TelegramLoginError(f"Could not send login code: {exc}") from exc

    _pending_logins[user_id] = {"client": client, "phone": phone, "started_at": time.monotonic()}
    return {"step": "code"}


async def submit_code(user_id: str, code: str) -> dict:
    """Step 2: submit the OTP code. Returns {"step": "ready"} or {"step": "password"}."""
    _drop_expired(user_id)
    entry = _pending_logins.get(user_id)
    if not entry:
        raise TelegramLoginError("No login in progress. Start over with a phone number.")

    client: TelegramClient = entry["client"]
    try:
        await client.sign_in(entry["phone"], code)
    except SessionPasswordNeededError:
        return {"step": "password"}
    except PhoneCodeInvalidError as exc:
        raise TelegramLoginError("That code was incorrect.") from exc
    except Exception as exc:
        await client.disconnect()
        _pending_logins.pop(user_id, None)
        raise TelegramLoginError(f"Login failed: {exc}") from exc

    return await _finalize_login(user_id)


async def submit_password(user_id: str, password: str) -> dict:
    """Step 3 (only if 2FA is enabled): submit the account's 2FA password."""
    _drop_expired(user_id)
    entry = _pending_logins.get(user_id)
    if not entry:
        raise TelegramLoginError("No login in progress. Start over with a phone number.")

    client: TelegramClient = entry["client"]
    try:
        await client.sign_in(password=password)
    except Exception as exc:
        raise TelegramLoginError(f"Incorrect password: {exc}") from exc

    return await _finalize_login(user_id)


async def _finalize_login(user_id: str) -> dict:
    entry = _pending_logins.pop(user_id)
    client: TelegramClient = entry["client"]
    session_string = client.session.save()
    await client.disconnect()

    await upsert_telegram_session_row(
        user_id,
        encrypted_session=_encrypt_session(session_string),
        phone=entry["phone"],
        status="connected",
        last_error=None,
    )
    return {"step": "ready"}


async def disconnect(user_id: str) -> None:
    await delete_telegram_session_row(user_id)


async def get_status(user_id: str) -> dict:
    row = await get_telegram_session_row(user_id)
    if not row:
        return {"connected": False}
    return {
        "connected": row.get("status") == "connected",
        "phone": row.get("phone"),
        "monitored_chats": row.get("monitored_chats") or [],
        "last_error": row.get("last_error"),
    }


async def _open_validated_client(user_id: str) -> TelegramClient:
    """
    Shared by both the short-lived (one tool call, then close) and the
    persistent (daemon listener, stays open) client paths -- the
    validation (session exists, is connected, is actually still
    authorized) must be identical either way. Only what happens to the
    client afterward (close immediately vs. hold it open) differs.
    """
    _require_app_credentials()
    row = await get_telegram_session_row(user_id)
    if not row or row.get("status") != "connected" or not row.get("encrypted_session"):
        raise TelegramLoginError("Telegram is not connected for this user.")

    session_string = _decrypt_session(row["encrypted_session"])
    client = TelegramClient(StringSession(session_string), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    try:
        await client.connect()
    except Exception as exc:
        raise TelegramLoginError(f"Could not open Telegram session: {exc}") from exc

    if not await client.is_user_authorized():
        # Session died silently (per the roadmap's own liveness warning) --
        # surface it as a clean reconnect-required error, not a crash.
        await client.disconnect()
        await upsert_telegram_session_row(
            user_id, status="disconnected", last_error="Session expired; reconnect required."
        )
        raise TelegramLoginError("Telegram session expired. Please reconnect.")

    return client


class _ShortLivedClient:
    """Opens a client from a stored session for one call, then disconnects."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._client: TelegramClient | None = None

    async def __aenter__(self) -> TelegramClient:
        self._client = await _open_validated_client(self.user_id)
        return self._client

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.disconnect()


async def open_persistent_client(user_id: str) -> TelegramClient:
    """
    For the agent-services listener daemon ONLY. Unlike
    `_ShortLivedClient`, the caller owns the client's lifetime -- it stays
    open (to receive live events via `client.add_event_handler`) until
    the caller explicitly calls `close_persistent_client`. Never use this
    for a one-off tool call; that's what `_ShortLivedClient` is for.
    """
    return await _open_validated_client(user_id)


async def close_persistent_client(client: TelegramClient) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


async def send_message(user_id: str, chat: str, text: str) -> str:
    """Send `text` to a chat the user's own Telegram account can reach.
    `chat` is a username, phone number, or chat ID -- resolved by Telethon.
    """
    async with _ShortLivedClient(user_id) as client:
        await client.send_message(chat, text)
    return f"Sent to {chat}."


async def list_chats(user_id: str, limit: int = 20) -> list[dict]:
    """List the user's recent dialogs (chats/groups/channels)."""
    async with _ShortLivedClient(user_id) as client:
        dialogs = await client.get_dialogs(limit=limit)
        return [
            {
                "id": d.id,
                "name": d.name,
                "is_group": d.is_group,
                "is_channel": d.is_channel,
                "unread_count": d.unread_count,
            }
            for d in dialogs
        ]