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