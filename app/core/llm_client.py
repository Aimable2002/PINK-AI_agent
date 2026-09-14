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
    api_key = os.environ.get(cfg["OPENROUTER_API_KEY"], "")
    if not api_key:
        raise ValueError(f"OPENROUTER_API_KEY is not set for tier {tier}")
    return await litellm.acompletion(
        model=cfg["model"],
        api_key=api_key,
        messages=messages,
        **kwargs,
    )
