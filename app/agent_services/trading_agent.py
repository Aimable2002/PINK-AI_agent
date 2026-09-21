from __future__ import annotations

import json
import asyncio
from collections import Counter
from typing import Any

import redis

from app.agent_services.base import AgentServicePaused
from app.config import (
    FORECAST_CACHE_TTL_SECONDS,
    FORECAST_COST_PER_CALL_USD,
    FORECAST_SIGNAL_CREDIT_COST,
    FORECASTING_ENABLED,
    REDIS_OPERATION_COST_USD,
    REDIS_URL,
    TOOL_CALL_CREDIT_SURCHARGE,
)
from app.connectors.manager import MCPConnectorManager
from app.core.forecast_client import forecast_price
from app.data.supabase_client import insert_trading_signal, touch_service_last_run
from app.queue.tasks import charge_usage, get_remaining_credits
from app.core.billing import usage_event

SERVICE_ID = "trading-agent"
DEFAULT_CONFIG = {
    "pair": None,
    "timeframe": None,
    "connector": "mt5",
    "candle_tool": "get_candles",
    "forecast_models": ["chronos2", "timesfm2_5", "moirai_moe"],
}


def _cache_key(pair: str, timeframe: str, connector: str, forecast_models: list[str]) -> str:
    sanitized_pair = (pair or "").strip().upper().replace(" ", "")
    sanitized_timeframe = (timeframe or "").strip().lower().replace(" ", "")
    sanitized_connector = (connector or "mt5").strip().lower().replace(" ", "")
    sanitized_models = ",".join(sorted(str(model).strip().lower() for model in forecast_models))
    return f"forecast:{sanitized_pair}:{sanitized_timeframe}:{sanitized_connector}:{sanitized_models}"


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


def _model_signal(item: dict, model: str) -> dict:
    direction = _normalize_direction(item.get("direction") or item.get("signal") or item.get("action"))
    return {
        "model": model,
        "direction": direction,
        "confidence": float(item.get("confidence", item.get("score", 0.0)) or 0.0),
        "entry": item.get("entry"),
        "take_profits": item.get("take_profits") or item.get("targets") or [],
        "stop_loss": item.get("stop_loss") or item.get("sl"),
        "raw": item,
    }


def _build_actionable_signal(pair: str, timeframe: str, model_forecasts: list[dict]) -> dict:
    combined = _combine_forecast_results(model_forecasts, "")
    directional = [item for item in model_forecasts if item.get("direction") == combined["direction"]]
    source = max(directional or model_forecasts, key=lambda item: item.get("confidence", 0.0))
    return {
        "symbol": pair.upper(),
        "signal_type": "forex",
        "direction": combined["direction"],
        "entry": source.get("entry"),
        "take_profits": source.get("take_profits") or [],
        "stop_loss": source.get("stop_loss"),
        "timeframe": timeframe,
        "confidence": combined["confidence"],
        "consensus": sum(item.get("direction") == combined["direction"] for item in model_forecasts),
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
    connector = str(config.get("connector") or DEFAULT_CONFIG["connector"]).strip().lower()
    candle_tool = str(config.get("candle_tool") or DEFAULT_CONFIG["candle_tool"]).strip()
    forecast_models = [str(model).strip() for model in (config.get("forecast_models") or DEFAULT_CONFIG["forecast_models"]) if str(model).strip()]
    forecast_model = ",".join(forecast_models)

    if not pair or not timeframe:
        raise ValueError("Trading agent is not configured: both pair and timeframe are required.")

    if not FORECASTING_ENABLED:
        raise AgentServicePaused("forecasting is not available in dev")
    if not connector_manager.is_registered(connector):
        raise AgentServicePaused(f"{connector} connector not registered")

    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    key = _cache_key(pair, timeframe, connector, forecast_models)
    usage_events = [usage_event(
        "redis",
        "redis",
        "cache_lookup",
        cost_usd=REDIS_OPERATION_COST_USD,
        metadata={"cache_key": key},
    )]
    cached_raw = cache.get(key)
    if cached_raw:
        cached = json.loads(cached_raw)
        usage_events.append(usage_event(
            "forecast_service",
            "backend",
            "trading_signal",
            credits=FORECAST_SIGNAL_CREDIT_COST,
            metadata={"pair": pair, "timeframe": timeframe, "cache_hit": True},
        ))
        charge_usage(user_id, {"usage_events": usage_events, "tiers_used": [forecast_model]}, task_id=service_row.get("_billing_task_id"))
        signal_row = insert_trading_signal(
            user_id=user_id,
            agent_service_id=service_row.get("id"),
            pair=pair,
            timeframe=timeframe,
            forecast_model=",".join(forecast_models),
            direction=cached.get("direction"),
            confidence=float(cached.get("confidence", 0.0) or 0.0),
            raw_forecast=cached.get("raw_forecast") or {},
            signal=cached.get("signal") or {},
            model_forecasts=cached.get("model_forecasts") or [],
        )
        return {
            "signal_id": signal_row["id"],
            "pair": pair,
            "timeframe": timeframe,
            "forecast_models": forecast_models,
            "direction": cached.get("direction", "neutral"),
            "confidence": float(cached.get("confidence", 0.0) or 0.0),
            "raw_forecast": cached.get("raw_forecast") or {},
            "cache_hit": True,
        }

    lock_key = f"{key}:lock"
    lock_acquired = cache.set(lock_key, "1", nx=True, ex=30)
    if lock_acquired:
        try:
            market_response = await connector_manager.call_tool(
                connector, candle_tool, {"pair": pair, "timeframe": timeframe}
            )
            usage_events.append(usage_event(
                "tool",
                connector,
                candle_tool,
                credits=TOOL_CALL_CREDIT_SURCHARGE,
                metadata={"pair": pair, "timeframe": timeframe},
            ))
            responses = await asyncio.gather(*[
                forecast_price(model, symbol=pair, timeframe=timeframe, candles=_extract_candles(market_response))
                for model in forecast_models
            ])
            usage_events.extend(
                usage_event(
                    "forecast_model",
                    "forecast_provider",
                    model,
                    cost_usd=FORECAST_COST_PER_CALL_USD.get(model, 0.0),
                    metadata={"pair": pair, "timeframe": timeframe},
                )
                for model in forecast_models
            )
            forecast_results = []
            for model, forecast_response in zip(forecast_models, responses):
                payload = forecast_response if isinstance(forecast_response, list) else [forecast_response]
                item = next((value for value in payload if isinstance(value, dict)), {})
                forecast_results.append(_model_signal(item, model))
            if not forecast_results:
                forecast_results.append({
                    "model": "ensemble",
                    "direction": "neutral",
                    "confidence": 0.0,
                    "raw": forecast_response,
                })

            combined = _combine_forecast_results(forecast_results, "")
            signal = _build_actionable_signal(pair, timeframe, forecast_results)
            signal = {
                "pair": pair,
                "timeframe": timeframe,
                "forecast_models": forecast_models,
                "direction": combined["direction"],
                "confidence": float(combined["confidence"] or 0.0),
                "raw_forecast": combined["raw_forecast"],
                "signal": signal,
                "model_forecasts": forecast_results,
            }
            cache.setex(key, _get_cache_ttl_seconds(timeframe), json.dumps(signal, default=str))
            signal_row = insert_trading_signal(
                user_id=user_id,
                agent_service_id=service_row.get("id"),
                pair=pair,
                timeframe=timeframe,
                forecast_model=",".join(forecast_models),
                direction=signal["direction"],
                confidence=signal["confidence"],
                raw_forecast=signal["raw_forecast"],
                signal=signal["signal"],
                model_forecasts=forecast_results,
            )
            usage_events.append(usage_event(
                "forecast_service",
                "backend",
                "trading_signal",
                credits=FORECAST_SIGNAL_CREDIT_COST,
                metadata={"pair": pair, "timeframe": timeframe},
            ))
            charge_usage(user_id, {
                "usage_events": usage_events,
                "steps": [],
                "tiers_used": [forecast_model],
            }, task_id=service_row.get("_billing_task_id"))
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
                usage_events.append(usage_event(
                    "forecast_service",
                    "backend",
                    "trading_signal",
                    credits=FORECAST_SIGNAL_CREDIT_COST,
                    metadata={"pair": pair, "timeframe": timeframe, "cache_hit": True},
                ))
                charge_usage(user_id, {"usage_events": usage_events, "tiers_used": [forecast_model]}, task_id=service_row.get("_billing_task_id"))
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


def _extract_candles(tool_result: Any) -> Any:
    """Convert an MCP result into JSON-shaped candle data for the model API."""
    if isinstance(tool_result, (dict, list)):
        return tool_result
    structured = getattr(tool_result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(tool_result, "content", None)
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except (TypeError, json.JSONDecodeError):
                    return text
    return str(tool_result)
