from __future__ import annotations

import os
from typing import Any

import httpx

from app.config import get_forecast_config


def _unwrap_response(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("output", "data", "predictions", "result"):
            if key in payload:
                return payload[key]
    return payload


async def forecast_price(
    model_name: str,
    *,
    symbol: str,
    timeframe: str,
    candles: Any,
    horizon: int = 1,
) -> Any:
    """Call a hosted forecasting model, outside the chat-completion path."""
    config = get_forecast_config(model_name)
    if config["provider"] == "huggingface":
        url = os.environ[config["hf_endpoint_url_env"]]
        token = os.environ[config["hf_token_env"]]
        body_key = "inputs"
    else:
        url = os.environ[config["runpod_url_env"]]
        token = os.environ[config["runpod_key_env"]]
        body_key = "input"

    request = {
        "model": model_name,
        "symbol": symbol,
        "timeframe": timeframe,
        "horizon": horizon,
        "candles": candles,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={body_key: request},
        )
        response.raise_for_status()
        return _unwrap_response(response.json())