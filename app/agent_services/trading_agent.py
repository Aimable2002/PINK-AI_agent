from __future__ import annotations

import json
from collections import Counter
from typing import Any

import redis

from app.agent_services.base import AgentServicePaused
from app.config import (
    FORECAST_CACHE_TTL_SECONDS,
    FORECAST_SIGNAL_CREDIT_COST,
    FORECASTING_ENABLED,
    REDIS_URL,
)
from app.connectors.manager import MCPConnectorManager
from app.data.supabase_client import insert_trading_signal, touch_service_last_run
from app.queue.tasks import charge_usage, get_remaining_credits

SERVICE_ID = "trading-agent"
DEFAULT_CONFIG = {"pair": None, "timeframe": None, "forecast_model": "kronos"}


def _cache_key(pair: str, timeframe: str, forecast_model: str) -> str:
    sanitized_pair = (pair or "").strip().upper().replace(" ", "")
    sanitized_timeframe = (timeframe or "").strip().lower().replace(" ", "")
    sanitized_model = (forecast_model or "kronos").strip().lower().replace(" ", "")
    return f"forecast:{sanitized_pair}:{sanitized_timeframe}:{sanitized_model}"


def _get_cache_ttl_seconds(timeframe: str) -> int:
    raw = (timeframe or "").strip().lower()
    if raw.endswith("m"):
        try:
            minutes = int(raw[:-1])
            return max(60, min(int(FORECAST_CACHE_TTL_SECONDS), max(60, minutes * 30)))
        except ValueError:
            pass
    return max(60, int(FORECAST_CACHE_TTL_SECONDS))


def _normalize_direction(value: Any) -> str:
    normalized = str(value or "neutral").strip().lower()
    return normalized if normalized in {"long", "short", "neutral"} else "neutral"


def _combine_forecast_results(results: list[dict], preferred_model: str) -> dict:
    if not results:
        return {
            "direction": "neutral",
            "confidence": 0.0,
            "model": preferred_model,
            "raw_forecast": {"results": []},
        }

    results = [dict(result) for result in results if isinstance(result, dict)]
    preferred = [r for r in results if str(r.get("model") or "").lower() == str(preferred_model).lower()]
    if preferred:
        chosen = preferred[0]
        return {
            "direction": _normalize_direction(chosen.get("direction", "neutral")),
            "confidence": float(chosen.get("confidence", 0.0) or 0.0),
            "model": str(chosen.get("model") or preferred_model),
            "raw_forecast": chosen.get("raw", chosen.get("raw_forecast", {})),
        }

    votes = Counter(_normalize_direction(r.get("direction")) for r in results)
    top_direction, _ = max(votes.items(), key=lambda item: (item[1], 1 if item[0] != "neutral" else 0))
    top_confidence = max(
        float(r.get("confidence", 0.0) or 0.0) for r in results if _normalize_direction(r.get("direction")) == top_direction
    )
    chosen = max(results, key=lambda r: (float(r.get("confidence", 0.0) or 0.0), 1 if _normalize_direction(r.get("direction")) == top_direction else 0))
    return {
        "direction": top_direction,
        "confidence": float(top_confidence or chosen.get("confidence", 0.0) or 0.0),
        "model": str(chosen.get("model") or preferred_model),
        "raw_forecast": chosen.get("raw", chosen.get("raw_forecast", {})),
    }


async def generate_signal(user_id: str, service_row: dict, connector_manager: MCPConnectorManager) -> dict:
    """
    Direct connector-to-connector pipeline for the trading agent. This path intentionally
    avoids the chat LLM and uses a shared Redis cache keyed by (pair, timeframe, model)
    so identical requests across users reuse one underlying compute result.
    """
    config = (service_row or {}).get("config") or {}
    pair = str(config.get("pair") or "").strip()
    timeframe = str(config.get("timeframe") or "").strip()
    forecast_model = str(config.get("forecast_model") or DEFAULT_CONFIG["forecast_model"]).strip() or DEFAULT_CONFIG["forecast_model"]

    if not pair or not timeframe:
        raise ValueError("Trading agent is not configured: both pair and timeframe are required.")

    if not FORECASTING_ENABLED:
        raise AgentServicePaused("forecasting is not available in dev")
    if not connector_manager.is_registered("forecasting"):
        raise AgentServicePaused("forecasting connector not registered")

    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    key = _cache_key(pair, timeframe, forecast_model)
    cached_raw = cache.get(key)
    if cached_raw:
        cached = json.loads(cached_raw)
        signal_row = insert_trading_signal(
            user_id=user_id,
            agent_service_id=service_row.get("id"),
            pair=pair,
            timeframe=timeframe,
            forecast_model=forecast_model,
            direction=cached.get("direction"),
            confidence=float(cached.get("confidence", 0.0) or 0.0),
            raw_forecast=cached.get("raw_forecast") or {},
        )
        return {
            "signal_id": signal_row["id"],
            "pair": pair,
            "timeframe": timeframe,
            "forecast_model": forecast_model,
            "direction": cached.get("direction", "neutral"),
            "confidence": float(cached.get("confidence", 0.0) or 0.0),
            "raw_forecast": cached.get("raw_forecast") or {},
            "cache_hit": True,
        }

    lock_key = f"{key}:lock"
    lock_acquired = cache.set(lock_key, "1", nx=True, ex=30)
    if lock_acquired:
        try:
            mt5_response = await connector_manager.call_tool("mt5", "get_candles", {"pair": pair, "timeframe": timeframe})
            forecast_response = await connector_manager.call_tool(
                "forecasting",
                "forecast_price",
                {"symbol": pair, "timeframe": timeframe, "horizon": 1, "model": forecast_model},
            )
            forecast_results = []
            payload = forecast_response if isinstance(forecast_response, list) else [forecast_response]
            for item in payload:
                if isinstance(item, dict):
                    forecast_results.append({
                        "model": item.get("model") or forecast_model,
                        "direction": _normalize_direction(item.get("direction") or item.get("signal") or item.get("action")),
                        "confidence": float(item.get("confidence", item.get("score", 0.0)) or 0.0),
                        "raw": item,
                    })
            if not forecast_results:
                forecast_results.append({
                    "model": forecast_model,
                    "direction": "neutral",
                    "confidence": 0.0,
                    "raw": forecast_response,
                })

            combined = _combine_forecast_results(forecast_results, forecast_model)
            signal = {
                "pair": pair,
                "timeframe": timeframe,
                "forecast_model": forecast_model,
                "direction": combined["direction"],
                "confidence": float(combined["confidence"] or 0.0),
                "raw_forecast": combined["raw_forecast"],
            }
            cache.setex(key, _get_cache_ttl_seconds(timeframe), json.dumps(signal, default=str))
            signal_row = insert_trading_signal(
                user_id=user_id,
                agent_service_id=service_row.get("id"),
                pair=pair,
                timeframe=timeframe,
                forecast_model=forecast_model,
                direction=signal["direction"],
                confidence=signal["confidence"],
                raw_forecast=signal["raw_forecast"],
            )
            from app.queue.tasks import charge_usage
            charge_usage(user_id, {"credits_used": FORECAST_SIGNAL_CREDIT_COST, "cost_usd": 0.0, "steps": [], "tiers_used": [forecast_model]})
            touch_service_last_run(service_row["id"])
            return {"signal_id": signal_row["id"], **signal, "cache_hit": False}
        finally:
            cache.delete(lock_key)
    else:
        # Another request is already computing this same signal; wait briefly for
        # the shared cache and then reuse the result rather than re-fetching MT5.
        for _ in range(25):
            cached_raw = cache.get(key)
            if cached_raw:
                cached = json.loads(cached_raw)
                signal_row = insert_trading_signal(
                    user_id=user_id,
                    agent_service_id=service_row.get("id"),
                    pair=pair,
                    timeframe=timeframe,
                    forecast_model=forecast_model,
                    direction=cached.get("direction"),
                    confidence=float(cached.get("confidence", 0.0) or 0.0),
                    raw_forecast=cached.get("raw_forecast") or {},
                )
                return {
                    "signal_id": signal_row["id"],
                    "pair": pair,
                    "timeframe": timeframe,
                    "forecast_model": forecast_model,
                    "direction": cached.get("direction", "neutral"),
                    "confidence": float(cached.get("confidence", 0.0) or 0.0),
                    "raw_forecast": cached.get("raw_forecast") or {},
                    "cache_hit": True,
                }
            import time
            time.sleep(0.2)

        signal_row = insert_trading_signal(
            user_id=user_id,
            agent_service_id=service_row.get("id"),
            pair=pair,
            timeframe=timeframe,
            forecast_model=forecast_model,
            direction="neutral",
            confidence=0.0,
            raw_forecast={"note": "cache miss after lock timeout"},
        )
        return {
            "signal_id": signal_row["id"],
            "pair": pair,
            "timeframe": timeframe,
            "forecast_model": forecast_model,
            "direction": "neutral",
            "confidence": 0.0,
            "raw_forecast": {"note": "cache miss after lock timeout"},
            "cache_hit": False,
        }
