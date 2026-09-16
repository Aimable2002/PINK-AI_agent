"""
The agent loop itself: call the model, execute any tool calls it requests
via the connected MCP servers, feed results back, repeat until the model
stops requesting tools or a safety cap is hit.

This does NOT decide what "good" or "done" means -- that judgment is the
model's own, driven entirely by the user's prompt (see conversation
history: the model interprets the goal, we don't hardcode an objective
function). This module only provides the mechanical loop and the safety
backstop (max iterations / max runtime) that prevents a runaway loop from
consuming unbounded compute if the model never converges on its own.
"""

import json
import time

from app.config import MAX_AGENT_ITERATIONS, MAX_AGENT_RUNTIME_SECONDS
from app.core.llm_client import call_tier, get_default_tools, web_search
from app.connectors.manager import MCPConnectorManager
from app.connectors.native_tools import NATIVE_CONNECTOR_IDS, call_native_tool, get_native_tool_schemas
from app.core.router import select_tier


class AgentRunResult:
    def __init__(self):
        self.steps: list[dict] = []
        self.final_message: str | None = None
        self.stopped_reason: str | None = None
        self.tiers_used: list[str] = []

    def to_dict(self) -> dict:
        tier = self.tiers_used[-1] if self.tiers_used else "medium"
        return {
            "steps": self.steps,
            "final_message": self.final_message,
            "stopped_reason": self.stopped_reason,
            "tiers_used": self.tiers_used,
            "tier": tier,
        }


def _extract_tool_calls(response) -> list[dict]:
    """
    Normalizes tool-call requests out of a LiteLLM/OpenAI-format response.
    Returns [] if the model produced a plain answer with no tool calls.
    """
    try:
        message = response.choices[0].message
    except (AttributeError, IndexError, KeyError, TypeError):
        return []

    tool_calls = getattr(message, "tool_calls", None) or []
    normalized = []
    for tc in tool_calls:
        normalized.append({
            "id": getattr(tc, "id", None),
            "name": tc.function.name,
            "arguments": tc.function.arguments,
        })
    return normalized


def _extract_text(response) -> str | None:
    try:
        return response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


async def run_agent_loop(
    prompt: str,
    messages: list[dict],
    connectors: list[str],
    connector_manager: MCPConnectorManager | None = None,
    call_tier_fn=call_tier,
    select_tier_fn=select_tier,
    mode: str = "chat",
    user_id: str | None = None,
) -> dict:
    """
    select_tier_fn defaults to the real RouteLLM-backed select_tier, which
    is NOT wired to a live classifier in this build (see router.py) and
    will raise NotImplementedError if called for real here. It's
    injectable so this loop's own mechanics (tool-call handling, safety
    caps, connector permission checks) can still be tested without a
    live trained router -- this does not reinvent or substitute for
    RouteLLM, it isolates what this file is responsible for from what
    router.py is responsible for.
    """
    if mode not in ("chat", "agent"):
        raise ValueError(f"Unknown mode: {mode!r}")

    result = AgentRunResult()
    connector_manager = connector_manager or MCPConnectorManager()

    conversation = list(messages) +  [{"role": "user", "content": prompt}]
    tools = get_default_tools(mode)
    tools.extend(get_native_tool_schemas(connectors))
    for connector_name in connectors:
        if connector_name in connector_manager._connectors:
            tools.extend(await connector_manager.list_tool_schemas(connector_name))

    start_time = time.monotonic()

    for iteration in range(MAX_AGENT_ITERATIONS):
        if time.monotonic() - start_time > MAX_AGENT_RUNTIME_SECONDS:
            result.stopped_reason = "max_runtime_exceeded"
            break

        tier = await select_tier_fn(prompt)
        result.tiers_used.append(tier)

        call_kwargs = {"tools": tools} if tools else {}
        try:
            response = await call_tier_fn(tier, conversation, **call_kwargs)
        except Exception as exc:
            result.final_message = f"error: {exc}"
            result.stopped_reason = "llm_call_failed"
            result.steps.append({
                "iteration": iteration,
                "tier": tier,
                "connector": "agent",
                "action": "llm_call_error",
                "detail": str(exc),
            })
            break

        tool_calls = _extract_tool_calls(response) if tools else []

        if not tool_calls:
            result.final_message = _extract_text(response)
            result.stopped_reason = "model_completed"
            result.steps.append({
                "iteration": iteration,
                "tier": tier,
                "connector": "agent",
                "action": "final_answer",
                "detail": result.final_message,
            })
            break

        conversation.append({
            "role": "assistant",
            "content": _extract_text(response),
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": (
                            call["arguments"]
                            if isinstance(call["arguments"], str)
                            else json.dumps(call["arguments"])
                        ),
                    },
                }
                for call in tool_calls
            ],
        })

        for call in tool_calls:
            call_name = call["name"]
            connector_name = call_name.split("__")[0] if "__" in call_name else None
            tool_name = call_name.split("__")[1] if "__" in call_name else call_name

            if call_name == "web_search":
                try:
                    arguments = call["arguments"]
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    tool_output = await web_search(arguments.get("query", ""))
                except Exception as exc:
                    tool_output = f"error: {exc}"
            elif call_name in ("telegram_send_message", "telegram_list_chats", "whatsapp_send_alert"):
                if not user_id:
                    tool_output = "error: no authenticated user for this native tool call"
                else:
                    try:
                        arguments = call["arguments"]
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                        tool_output = await call_native_tool(user_id, call_name, arguments)
                    except Exception as exc:
                        tool_output = f"error: {exc}"
            elif connector_name is not None and connector_name in connectors and connector_name not in NATIVE_CONNECTOR_IDS:
                allowed, denied_scope = connector_manager.tool_scope_allowed(connector_name, tool_name)
                if not allowed:
                    tool_output = (
                        f"error: tool '{tool_name}' blocked by scope '{denied_scope}' for connector '{connector_name}'"
                    )
                else:
                    try:
                        arguments = call["arguments"]
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                        tool_result = await connector_manager.call_tool(
                            connector_name, tool_name, arguments
                        )
                        tool_output = str(tool_result)
                    except Exception as exc:
                        tool_output = f"error: {exc}"
            elif connector_name is not None:
                tool_output = f"error: connector '{connector_name}' not connected for this user"
            else:
                tool_output = f"error: unknown tool '{call_name}'"

            result.steps.append({
                "iteration": iteration,
                "tier": tier,
                "connector": connector_name or "agent",
                "action": tool_name,
                "detail": tool_output,
            })
            conversation.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": tool_output,
            })
    else:
        result.stopped_reason = "max_iterations_reached"

    return result.to_dict()