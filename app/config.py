import os
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))


def _get_mode() -> str:
    mode = os.environ.get("MODE", "dev").lower()
    if mode not in ("dev", "prod"):
        raise ValueError(f"MODE must be 'dev' or 'prod', got {mode!r}")
    return mode


MODE = _get_mode()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")


TIER_CONFIGS = {
    "dev": {
        "classifier": {
            "model": "openrouter/inclusionai/ling-3.0-flash-vl:free",
            "api_base": None,
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "small": {
            "model": "openrouter/nex-agi/nex-n2.5-mini:free",
            "api_base": None,
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "medium": {
            "model": "openrouter/nex-agi/nex-n2.5-pro:free",
            "api_base": None,
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "best": {
            "model": "openrouter/poolside/laguna-s-2.1:free",
            "api_base": None,
            "api_key_env": "OPENROUTER_API_KEY",
        },
    },
    "prod": {
        "classifier": {
            "model": "openai/qwen3.6-4b",
            "api_base_env": "RUNPOD_CLASSIFIER_URL",
            "api_key_env": "RUNPOD_CLASSIFIER_KEY",
        },
        "small": {
            "model": "openai/qwen3.6-4b",
            "api_base_env": "RUNPOD_SMALL_URL",
            "api_key_env": "RUNPOD_SMALL_KEY",
        },
        "medium": {
            "model": "openai/deepseek-v4-flash",
            "api_base_env": "RUNPOD_MEDIUM_URL",
            "api_key_env": "RUNPOD_MEDIUM_KEY",
        },
        "best": {
            "model": "openai/glm-5.2",
            "api_base_env": "RUNPOD_BEST_URL",
            "api_key_env": "RUNPOD_BEST_KEY",
        },
    },
}

TIER_THRESHOLDS = {
    "small_to_medium": float(os.environ.get("THRESH_SMALL_MEDIUM", "0.3")),
    "medium_to_best": float(os.environ.get("THRESH_MEDIUM_BEST", "0.7")),
}


def _normalize_redis_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "rediss":
        return url

    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "ssl_cert_reqs" for key, _ in query):
        query.append(("ssl_cert_reqs", "CERT_REQUIRED"))

    return urlunsplit(parsed._replace(query=urlencode(query)))


REDIS_URL = _normalize_redis_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379/0")
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

MAX_AGENT_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "10"))
MAX_AGENT_RUNTIME_SECONDS = int(os.environ.get("MAX_AGENT_RUNTIME_SECONDS", "600"))
FREE_QUEUE_MAX_DEPTH = int(os.environ.get("FREE_QUEUE_MAX_DEPTH", "50"))

TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_ENCRYPTION_KEY = os.environ.get("TELEGRAM_SESSION_ENCRYPTION_KEY", "")

WHATSAPP_GRAPH_API_VERSION = os.environ.get("WHATSAPP_GRAPH_API_VERSION", "v21.0")
WHATSAPP_DEFAULT_TEMPLATE_NAME = os.environ.get("WHATSAPP_DEFAULT_TEMPLATE_NAME", "hello_world")
WHATSAPP_DEFAULT_TEMPLATE_LANG = os.environ.get("WHATSAPP_DEFAULT_TEMPLATE_LANG", "en_US")

# --- Credit system ---------------------------------------------------
# 1 credit = $0.01 of real model cost by default. `profiles.quota_limit`
# is denominated in credits under this scheme -- the default of 500
# therefore means "$5/month of real model spend" for the free tier, not
# "500 requests" as the old flat-rate system implied.
CREDITS_PER_USD = float(os.environ.get("CREDITS_PER_USD", "100"))

# litellm.completion_cost() only knows prices for models in its own
# pricing table. A custom RunPod-hosted model (the whole `prod` MODE)
# will not be in that table and completion_cost() will raise or return
# 0 -- silently undercharging every prod request otherwise. This is the
# required fallback: a fixed $/1K-token rate per tier, used only when
# litellm can't price the call itself. Keep these current with your
# actual RunPod GPU-hour cost divided by realistic throughput; they are
# a deliberate estimate, not a discovered price.
FALLBACK_COST_PER_1K_TOKENS_USD = {
    "classifier": float(os.environ.get("FALLBACK_COST_CLASSIFIER", "0.0005")),
    "small": float(os.environ.get("FALLBACK_COST_SMALL", "0.001")),
    "medium": float(os.environ.get("FALLBACK_COST_MEDIUM", "0.004")),
    "best": float(os.environ.get("FALLBACK_COST_BEST", "0.015")),
}

# Flat credit surcharge per tool call, on top of token cost -- a run that
# hits four connectors costs more in latency/API calls than one that
# doesn't, even if token usage is identical.
TOOL_CALL_CREDIT_SURCHARGE = float(os.environ.get("TOOL_CALL_CREDIT_SURCHARGE", "0.5"))


def get_tier_config(tier: str) -> dict:
    if tier not in ("classifier", "small", "medium", "best"):
        raise ValueError(f"Unknown tier: {tier!r}")

    cfg = dict(TIER_CONFIGS[MODE][tier])

    if MODE == "prod":
        api_base = os.environ.get(cfg.pop("api_base_env"))
        if not api_base:
            raise RuntimeError(
                f"MODE=prod but no RunPod URL set for tier '{tier}'. "
                f"Set the corresponding env var before serving prod traffic."
            )
        cfg["api_base"] = api_base

    return cfg