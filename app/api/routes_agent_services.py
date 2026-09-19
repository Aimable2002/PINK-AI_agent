from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agent_services import REGISTRY
from app.agent_services.telegram_signal_monitor import DEFAULT_CONFIG, SERVICE_ID
from app.api.dependencies import get_current_user
from app.data.supabase_client import UserContext, get_agent_service, get_client, upsert_agent_service

router = APIRouter(prefix="/v1/agent-services", tags=["agent-services"])


class TelegramSignalMonitorConfig(BaseModel):
    monitored_chats: list[str] = []
    alert_chat: str = DEFAULT_CONFIG["alert_chat"]


@router.get("/telegram-signal-monitor")
async def get_config(user: UserContext = Depends(get_current_user)):
    row = await get_agent_service(user.user_id, SERVICE_ID)
    if not row:
        return {"status": "paused", "config": DEFAULT_CONFIG}
    return {"status": row["status"], "config": row.get("config") or DEFAULT_CONFIG, "paused_reason": row.get("paused_reason")}


@router.put("/telegram-signal-monitor")
async def save_config(payload: TelegramSignalMonitorConfig, user: UserContext = Depends(get_current_user)):
    if SERVICE_ID not in REGISTRY:
        raise HTTPException(status_code=500, detail="service not registered")
    row = await upsert_agent_service(user.user_id, SERVICE_ID, payload.model_dump())
    return {"status": row.get("status", "paused"), "config": row.get("config")}


@router.post("/telegram-signal-monitor/activate")
async def activate(user: UserContext = Depends(get_current_user)):
    row = await get_agent_service(user.user_id, SERVICE_ID)
    if not row or not (row.get("config") or {}).get("monitored_chats"):
        raise HTTPException(status_code=400, detail="Select at least one chat to monitor before activating.")
    updated = await upsert_agent_service(user.user_id, SERVICE_ID, row.get("config") or {}, status="active")
    return {"status": updated.get("status", "active")}


@router.post("/telegram-signal-monitor/pause")
async def pause(user: UserContext = Depends(get_current_user)):
    row = await get_agent_service(user.user_id, SERVICE_ID)
    updated = await upsert_agent_service(user.user_id, SERVICE_ID, (row or {}).get("config") or {}, status="paused")
    return {"status": updated.get("status", "paused")}


@router.get("/telegram-signal-monitor/signals")
async def recent_signals(limit: int = 50, user: UserContext = Depends(get_current_user)):
    client = get_client()
    resp = (
        client.table("signals")
        .select("*")
        .eq("user_id", user.user_id)
        .order("created_at", desc=True)
        .limit(min(limit, 200))
        .execute()
    )
    return {"signals": resp.data or []}