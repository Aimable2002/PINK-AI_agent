from dataclasses import dataclass

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
    client = get_client()
    response = (
        client.table("tasks")
        .select("id, user_id, job_id, status")
        .eq("job_id", job_id)
        .maybe_single()
        .execute()
    )
    return response.data


def update_task_by_job_id(job_id: str, **fields) -> None:
    if not fields:
        return
    get_client().table("tasks").update(fields).eq("job_id", job_id).execute()


def record_usage(
    user_id: str,
    tier: str,
    task_id: str | None = None,
    requests: int = 1,
    tool_calls: int = 0,
    cost_usd: float = 0,
) -> None:
    client = get_client()
    if task_id:
        existing = (
            client.table("usage_events")
            .select("id")
            .eq("task_id", task_id)
            .maybe_single()
            .execute()
        )
        if existing is not None and existing.data:
            return
    client.rpc(
        "increment_quota",
        {"_user_id": user_id, "_requests": requests},
    ).execute()
    client.table("usage_events").insert(
        {
            "user_id": user_id,
            "task_id": task_id,
            "tier": tier,
            "requests": requests,
            "tool_calls": tool_calls,
            "cost_usd": cost_usd,
        }
    ).execute()


# ------------------------------------------------------------------ telegram
# telegram_sessions is intentionally its own table, not mcp_connections --
# a Telethon session doesn't have a url/transport/auth_header shape, it's a
# single opaque encrypted blob plus login state.

async def get_telegram_session_row(user_id: str) -> dict | None:
    client = get_client()
    resp = (
        client.table("telegram_sessions")
        .select("*")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    return resp.data if resp else None


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
    client = get_client()
    resp = (
        client.table("whatsapp_credentials")
        .select("*")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    return resp.data