import json
import os

import httpx
import litellm

from app.config import FALLBACK_COST_PER_1K_TOKENS_USD, MODE, get_tier_config


def get_default_tools(mode: str | None = None) -> list[dict]:
    """Return the default backend tool set for the current runtime mode."""
    requested_mode = (mode or MODE).lower()
    active_mode = MODE.lower() if requested_mode in {"chat", "agent"} else requested_mode
    if active_mode == "dev":
        # return [{"type": "openrouter:web_search"}]
        return [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for current information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The exact search query to run.",
                        }
                    },
                    "required": ["query"],
                },
            },
        }]

    if active_mode == "prod":
        return [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for current information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The exact search query to run.",
                        }
                    },
                    "required": ["query"],
                },
            },
        }]

    raise ValueError(f"Unknown MODE: {active_mode!r}")


async def web_search(query: str) -> str:
    """Execute a backend web search for the prod path."""
    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        return "error: SERPER_API_KEY is not configured for web_search."

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            content=json.dumps({"q": query}),
        )
        response.raise_for_status()
        payload = response.json()

    organic = payload.get("organic", [])
    if not organic:
        return "No web search results found."

    formatted = [
        f"{idx + 1}. {result.get('title', 'Untitled')} - {result.get('snippet', '')} ({result.get('link', '')})"
        for idx, result in enumerate(organic[:5])
    ]
    return "\n".join(formatted)


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


def extract_usage(response, tier: str) -> dict:
    """
    Pull real token counts and real USD cost out of a LiteLLM response.
    This is the actual metering mechanism for the credit system -- every
    call already carries this data, it was just never being read.

    litellm.completion_cost() prices the call using litellm's own model
    cost table when the model is in it (true for hosted providers like
    OpenRouter's upstream models). For a custom RunPod-hosted model
    (the whole `prod` MODE), that table has no entry, so completion_cost()
    raises or silently returns 0 -- caught here and replaced with the
    configured fallback $/1K-token estimate for that tier, rather than
    ever letting a call be metered as free.
    """
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)

    cost_usd = 0.0
    priced_by_litellm = False
    try:
        computed = litellm.completion_cost(completion_response=response)
        if computed and computed > 0:
            cost_usd = float(computed)
            priced_by_litellm = True
    except Exception:
        pass

    if not priced_by_litellm:
        rate = FALLBACK_COST_PER_1K_TOKENS_USD.get(tier, FALLBACK_COST_PER_1K_TOKENS_USD["medium"])
        cost_usd = (total_tokens / 1000.0) * rate

    return {
        "tier": tier,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": round(cost_usd, 6),
        "priced_by_litellm": priced_by_litellm,
    }