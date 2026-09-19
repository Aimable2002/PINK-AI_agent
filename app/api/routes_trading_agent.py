from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agent_services import REGISTRY
from app.agent_services.trading_agent import DEFAULT_CONFIG, SERVICE_ID
from app.api.dependencies import get_current_user
from app.data.supabase_client import UserContext, get_agent_service, get_client, list_trading_signals, upsert_agent_service
from app.queue.agent_tasks import generate_signal_job

router = APIRouter(prefix="/v1/agent-services", tags=["agent-services"])


class TradingAgentConfig(BaseModel):
    pair: str | None = None
    timeframe: str | None = None
    connector: str = DEFAULT_CONFIG["connector"]
    candle_tool: str = DEFAULT_CONFIG["candle_tool"]
    forecast_models: list[str] = DEFAULT_CONFIG["forecast_models"]


@router.get("/trading-agent")
async def get_config(user: UserContext = Depends(get_current_user)):
    row = await get_agent_service(user.user_id, SERVICE_ID)
    if not row:
        return {"status": "paused", "config": DEFAULT_CONFIG}
    return {"status": row["status"], "config": row.get("config") or DEFAULT_CONFIG, "paused_reason": row.get("paused_reason")}


@router.put("/trading-agent")
async def save_config(payload: TradingAgentConfig, user: UserContext = Depends(get_current_user)):
    if SERVICE_ID not in REGISTRY:
        raise HTTPException(status_code=500, detail="service not registered")
    row = await upsert_agent_service(user.user_id, SERVICE_ID, payload.model_dump())
    return {"status": row.get("status", "paused"), "config": row.get("config")}


@router.post("/trading-agent/activate")
async def activate(user: UserContext = Depends(get_current_user)):
    row = await get_agent_service(user.user_id, SERVICE_ID)
    config = (row or {}).get("config") or {}
    if not config.get("pair") or not config.get("timeframe"):
        raise HTTPException(status_code=400, detail="Set both pair and timeframe before activating the Trading Agent.")
    updated = await upsert_agent_service(user.user_id, SERVICE_ID, config, status="active")
    return {"status": updated.get("status", "active")}


@router.post("/trading-agent/pause")
async def pause(user: UserContext = Depends(get_current_user)):
    row = await get_agent_service(user.user_id, SERVICE_ID)
    updated = await upsert_agent_service(user.user_id, SERVICE_ID, (row or {}).get("config") or {}, status="paused")
    return {"status": updated.get("status", "paused")}


@router.post("/trading-agent/generate")
async def generate(payload: TradingAgentConfig | None = None, user: UserContext = Depends(get_current_user)):
    row = await get_agent_service(user.user_id, SERVICE_ID)
    if not row:
        raise HTTPException(status_code=400, detail="Trading Agent is not configured.")
    config = (row.get("config") or {})
    pair = (payload.pair if payload else config.get("pair")) or config.get("pair")
    timeframe = (payload.timeframe if payload else config.get("timeframe")) or config.get("timeframe")
    connector = (payload.connector if payload else config.get("connector")) or config.get("connector") or DEFAULT_CONFIG["connector"]
    candle_tool = (payload.candle_tool if payload else config.get("candle_tool")) or config.get("candle_tool") or DEFAULT_CONFIG["candle_tool"]
    forecast_models = (payload.forecast_models if payload else config.get("forecast_models")) or config.get("forecast_models") or DEFAULT_CONFIG["forecast_models"]
    if not pair or not timeframe:
        raise HTTPException(status_code=400, detail="Set both pair and timeframe before generating a signal.")
    service_row = {**row, "config": {**config, "pair": pair, "timeframe": timeframe, "connector": connector, "candle_tool": candle_tool, "forecast_models": forecast_models}}
    job = generate_signal_job.apply_async(args=[user.user_id, service_row], queue="free_standard")
    return {"job_id": job.id, "status": "queued"}


@router.get("/trading-agent/signals")
async def recent_signals(limit: int = 50, user: UserContext = Depends(get_current_user)):
    return {"signals": list_trading_signals(user.user_id, limit)}
