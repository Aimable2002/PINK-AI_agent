import json
import os

import httpx
import litellm

from app.config import FALLBACK_COST_PER_1K_TOKENS_USD, LITELLM_DEBUG, MODE, get_tier_config
from app.core.billing import usage_event


if LITELLM_DEBUG:
    litellm._turn_on_debug()


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
        }, {
            "type": "function",
            "function": {
                "name": "browser_use",
                "description": "Open a web page and extract information for a user task.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The browser task to perform, such as 'get the current price of BTCUSD'.",
                        }
                    },
                    "required": ["task"],
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
        }, {
            "type": "function",
            "function": {
                "name": "browser_use",
                "description": "Open a web page and extract information for a user task.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The browser task to perform, such as 'get the current price of BTCUSD'.",
                        }
                    },
                    "required": ["task"],
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


def search_usage_event(query: str, *, result_count: int = 0) -> dict:
    """Return the configured per-request search charge for the billing ledger."""
    return usage_event(
        "search",
        "serper",
        "web_search",
        cost_usd=float(os.environ.get("SERPER_COST_USD", "0.0025")),
        metadata={"query_length": len(query), "result_count": result_count},
    )


async def browser_use(task: str) -> str:
    """Browser-use shim for the assistant tool surface. The real implementation can be
    wired to a package later; this keeps the runtime contract open without breaking tests."""
    if not task or not str(task).strip():
        return "error: browser_use task is empty."
    return f"browser_use not configured for task: {task}"


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
