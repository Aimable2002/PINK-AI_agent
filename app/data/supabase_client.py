from dataclasses import dataclass
from datetime import datetime, timezone

from supabase import create_client, Client

from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY


@dataclass
class UserContext:
    user_id: str
    plan: str
    quota_limit: int
    quota_used: int

    @property
    def quota_exceeded(self) -> bool:
        return self.quota_used >= self.quota_limit


_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY not set. "
                "Required for any real Supabase-backed call."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


async def verify_token_and_get_user(jwt: str) -> str:
    """Returns the user_id for a valid Supabase Auth JWT, raises on failure."""
    client = get_client()
    user_response = client.auth.get_user(jwt)
    if not user_response or not user_response.user:
        raise ValueError("Invalid or expired token")
    return user_response.user.id


async def get_user_context(user_id: str) -> UserContext:
    client = get_client()
    resp = (
        client.table("profiles")
        .select("plan, quota_limit, quota_used")
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    row = resp.data
    return UserContext(
        user_id=user_id,
        plan=row["plan"],
        quota_limit=row["quota_limit"],
        quota_used=row["quota_used"],
    )


def get_task_by_job_id(job_id: str) -> dict | None:
    """No row is a normal outcome (unknown/expired job_id), not an error --
    see get_agent_service's docstring for why .maybe_single() can't be used
    for that."""
    client = get_client()
    response = (
        client.table("tasks")
        .select("id, user_id, job_id, status")
        .eq("job_id", job_id)
        .execute()
    )
    return response.data[0] if response.data else None


def update_task_by_job_id(job_id: str, **fields) -> None:
    if not fields:
        return
    get_client().table("tasks").update(fields).eq("job_id", job_id).execute()


def record_usage(
    user_id: str,
    tier: str,
    task_id: str | None = None,
    requests: float = 1,
    tool_calls: int = 0,
    cost_usd: float = 0,
    usage_events: list[dict] | None = None,
) -> None:
    client = get_client()
    if task_id:
        # No existing row is the common case -- this check runs on every
        # call, and only finds a match on a retry/duplicate. Plain select,
        # not .maybe_single() (see get_agent_service's docstring).
        existing = (
            client.table("usage_events")
            .select("id")
            .eq("task_id", task_id)
            .execute()
        )
        if existing.data:
            return
    credits = sum(float(event.get("credits", 0.0) or 0.0) for event in (usage_events or []))
    if not credits:
        credits = float(requests)
    client.rpc(
        "charge_usage",
        {"_user_id": user_id, "_credits": credits},
    ).execute()
    client.table("usage_events").insert(
        {
            "user_id": user_id,
            "task_id": task_id,
            "tier": tier,
            "requests": requests,
            "tool_calls": tool_calls,
            "cost_usd": cost_usd,
            "credits": credits,
            "usage_events": usage_events or [],
        }
    ).execute()


# ------------------------------------------------------------------ telegram
# telegram_sessions is intentionally its own table, not mcp_connections --
# a Telethon session doesn't have a url/transport/auth_header shape, it's a
# single opaque encrypted blob plus login state.

async def get_telegram_session_row(user_id: str) -> dict | None:
    """No row is normal for a user who hasn't connected Telegram (see
    get_agent_service's docstring for why .maybe_single() can't be used
    for that)."""
    client = get_client()
    resp = (
        client.table("telegram_sessions")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )
    return resp.data[0] if resp.data else None


async def upsert_telegram_session_row(user_id: str, **fields) -> None:
    client = get_client()
    row = {"user_id": user_id, **fields}
    client.table("telegram_sessions").upsert(row, on_conflict="user_id").execute()


async def delete_telegram_session_row(user_id: str) -> None:
    client = get_client()
    client.table("telegram_sessions").delete().eq("user_id", user_id).execute()


# ------------------------------------------------------------------ whatsapp
# whatsapp_credentials is also its own table -- three separate values
# (access token, phone number id, business account id) plus the recipient
# number, not a single bearer token against a shared URL.

async def get_whatsapp_credentials_row(user_id: str) -> dict | None:
    """No row is normal for a user who hasn't connected WhatsApp (see
    get_agent_service's docstring for why .maybe_single() can't be used
    for that)."""
    client = get_client()
    resp = (
        client.table("whatsapp_credentials")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )
    return resp.data[0] if resp.data else None


# ------------------------------------------------------------- agent services
# One row per (user, service_id). `config` is opaque to this layer -- each
# agent module (app/agent_services/<name>.py) owns and validates the shape
# of its own config; this file just persists whatever dict it's given.

def get_active_services(service_id: str) -> list[dict]:
    """All users' active rows for one agent service type. Used by the
    daemon at startup and on its periodic reconciliation pass -- NOT
    async, since the daemon calls this from a plain sync loop tick."""
    client = get_client()
    resp = (
        client.table("user_agent_services")
        .select("*")
        .eq("service_id", service_id)
        .eq("status", "active")
        .execute()
    )
    return resp.data or []


async def get_agent_service(user_id: str, service_id: str) -> dict | None:
    """Row is expected to be absent for a user who has never configured this
    service -- that's the normal "not configured" state, not an error. Plain
    select + manual unwrap instead of .maybe_single(): PostgREST returns 406
    (not 200-with-empty-body) when its object Accept header matches zero
    rows, which makes postgrest-py raise APIError instead of returning None,
    even via maybe_single()."""
    client = get_client()
    resp = (
        client.table("user_agent_services")
        .select("*")
        .eq("user_id", user_id)
        .eq("service_id", service_id)
        .execute()
    )
    return resp.data[0] if resp.data else None


async def upsert_agent_service(user_id: str, service_id: str, config: dict, status: str | None = None) -> dict:
    client = get_client()
    row = {"user_id": user_id, "service_id": service_id, "config": config}
    if status is not None:
        row["status"] = status
    resp = client.table("user_agent_services").upsert(row, on_conflict="user_id,service_id").execute()
    return (resp.data or [{}])[0]


def set_service_status(service_row_id: str, status: str, paused_reason: str | None = None) -> None:
    """Sync, not async -- called from the daemon's plain asyncio loop and
    from Celery tasks, neither of which need to await a Supabase client
    that is itself synchronous under the hood."""
    client = get_client()
    fields = {"status": status, "paused_reason": paused_reason, "updated_at": datetime.now(timezone.utc).isoformat()}
    client.table("user_agent_services").update(fields).eq("id", service_row_id).execute()


def touch_service_last_run(service_row_id: str) -> None:
    client = get_client()
    client.table("user_agent_services").update(
        {"last_run_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", service_row_id).execute()


# ------------------------------------------------------------------- signals

def insert_signal(
    user_id: str,
    agent_service_id: str,
    channel: str | None,
    raw_text: str,
    source: str = "telegram",
) -> dict:
    client = get_client()
    resp = client.table("signals").insert({
        "user_id": user_id,
        "agent_service_id": agent_service_id,
        "source": source,
        "channel": channel,
        "raw_text": raw_text,
    }).execute()
    return (resp.data or [{}])[0]


def update_signal(signal_id: str, **fields) -> None:
    if not fields:
        return
    client = get_client()
    client.table("signals").update(fields).eq("id", signal_id).execute()


# ------------------------------------------------------------------- trading

def insert_trading_signal(
    user_id: str,
    agent_service_id: str | None,
    pair: str,
    timeframe: str,
    forecast_model: str,
    direction: str | None,
    confidence: float | None,
    raw_forecast: dict | None,
    signal: dict | None = None,
    model_forecasts: list[dict] | None = None,
) -> dict:
    client = get_client()
    resp = client.table("trading_signals").insert({
        "user_id": user_id,
        "agent_service_id": agent_service_id,
        "pair": pair,
        "timeframe": timeframe,
        "forecast_model": forecast_model,
        "direction": direction,
        "confidence": confidence,
        "raw_forecast": raw_forecast or {},
        "signal": signal or {},
        "model_forecasts": model_forecasts or [],
    }).execute()
    return (resp.data or [{}])[0]


def list_trading_signals(user_id: str, limit: int = 50) -> list[dict]:
    client = get_client()
    resp = (
        client.table("trading_signals")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(min(limit, 200))
        .execute()
    )
    return resp.data or []