import os
from typing import Optional


def _get_mode() -> str:
    mode = os.environ.get("MODE", "dev").lower()
    if mode not in ("dev", "prod"):
        raise ValueError(f"MODE must be 'dev' or 'prod', got {mode!r}")
    return mode


MODE = _get_mode()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# Model choices below reflect the recommendation discussed: Qwen3.6 (small),
# DeepSeek V4 Flash (medium), GLM-5.2 (best) -- current strongest
# open-weight options as of this build. VERIFY exact OpenRouter slug
# strings against https://openrouter.ai/models before deploying --
# slugs shift as providers publish new listings, and these have not
# been confirmed against a live OpenRouter account in this environment.

TIER_CONFIGS = {
    "dev": {
        "classifier": {
            "model": "inclusionai/ling-3.0-flash-vl:free",
            "api_base": None,
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "small": {
            "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "api_base": None,
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "medium": {
            "model": "nex-agi/nex-n2.5-pro:free",
            "api_base": None,
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "best": {
            "model": "poolside/laguna-s-2.1:free",
            "api_base": None,
            "api_key_env": "OPENROUTER_API_KEY",
        },
    },
    "prod": {
        "classifier": {
            "model": "hosted/qwen3.6-4b",
            "api_base_env": "RUNPOD_CLASSIFIER_URL",
            "api_key_env": "RUNPOD_CLASSIFIER_KEY",
        },
        "small": {
            "model": "hosted/qwen3.6-4b",
            "api_base_env": "RUNPOD_SMALL_URL",
            "api_key_env": "RUNPOD_SMALL_KEY",
        },
        "medium": {
            "model": "hosted/deepseek-v4-flash",
            "api_base_env": "RUNPOD_MEDIUM_URL",
            "api_key_env": "RUNPOD_MEDIUM_KEY",
        },
        "best": {
            "model": "hosted/glm-5.2",
            "api_base_env": "RUNPOD_BEST_URL",
            "api_key_env": "RUNPOD_BEST_KEY",
        },
    },
}

TIER_THRESHOLDS = {
    "small_to_medium": float(os.environ.get("THRESH_SMALL_MEDIUM", "0.3")),
    "medium_to_best": float(os.environ.get("THRESH_MEDIUM_BEST", "0.7")),
}

REDIS_URL = os.environ.get("REDIS_URL")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

MAX_AGENT_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "10"))
MAX_AGENT_RUNTIME_SECONDS = int(os.environ.get("MAX_AGENT_RUNTIME_SECONDS", "600"))


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
