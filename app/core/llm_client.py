import os

import litellm

from app.config import get_tier_config


async def call_tier(tier: str, messages: list[dict], **kwargs) -> dict:
    """
    Single entry point for every model call, regardless of tier or MODE.
    Swapping dev(OpenRouter) <-> prod(RunPod) never touches this function --
    only config.get_tier_config()'s output changes.
    """
    cfg = get_tier_config(tier)
    api_key = os.environ.get(cfg["api_key_env"], "")

    request_kwargs = {
        "model": cfg["model"],
        "api_key": api_key,
        "messages": messages,
        "timeout": kwargs.pop("timeout", 60),
        **kwargs,
    }
    if cfg.get("api_base"):
        request_kwargs["api_base"] = cfg["api_base"]

    response = await litellm.acompletion(**request_kwargs)
    return response
