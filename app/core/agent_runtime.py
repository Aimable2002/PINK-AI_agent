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
import asyncio

from app.config import MAX_AGENT_ITERATIONS, TOOL_CALL_CREDIT_SURCHARGE
from app.core.billing import sum_usage, usage_event
from app.core.llm_client import browser_use, call_tier, get_default_tools, model_usage_event, search_usage_event, web_search
from app.connectors.manager import MCPConnectorManager
from app.connectors.native_tools import NATIVE_CONNECTOR_IDS, call_native_tool, get_native_tool_schemas
from app.core.router import select_tier


class AgentRunResult:
    def __init__(self):
        self.steps: list[dict] = []
        self.final_message: str | None = None
        self.stopped_reason: str | None = None
        self.tiers_used: list[str] = []
        # Real measured usage, not a flat per-job guess -- see
        # llm_client.extract_usage(). credits_used is what actually gets
        # charged against the user's balance; cost_usd is kept alongside
        # it purely for finance/auditing visibility in usage_events.
        self.cost_usd: float = 0.0
        self.credits_used: float = 0.0
        self.usage_events: list[dict] = []

    def to_dict(self) -> dict:
        tier = self.tiers_used[-1] if self.tiers_used else "medium"
        return {
            "steps": self.steps,
            "final_message": self.final_message,
            "stopped_reason": self.stopped_reason,
            "tiers_used": self.tiers_used,
            "tier": tier,
            "cost_usd": round(self.cost_usd, 6),
            "credits_used": round(self.credits_used, 4),
            "usage_events": self.usage_events,
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
    credit_budget: float | None = None,
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

    credit_budget is the user's remaining balance in credits, fetched
    once by the caller (see tasks.py) before this loop starts. It is
    checked before every iteration -- not just once at job start -- so a
    long tool-calling run stops the moment it exhausts the budget rather
    than completing on credit it doesn't have. Pass None to run with no
    budget enforcement (e.g. in tests).
    """
    if mode not in ("chat", "agent"):
        raise ValueError(f"Unknown mode: {mode!r}")

    result = AgentRunResult()
    connector_manager = connector_manager or MCPConnectorManager()

    conversation = list(messages)
    tools = get_default_tools(mode)
    tools.extend(get_native_tool_schemas(connectors))
    unavailable_connectors: set[str] = set()
    unavailable_details: dict[str, str] = {}
    for connector_name in connectors:
        if connector_name in connector_manager._connectors:
            try:
                # Tool discovery is required to give the model valid schemas,
                # but a connector being temporarily unavailable must not abort
                # the whole run or prevent other tools from being used.
                tools.extend(await asyncio.wait_for(
                    connector_manager.list_tool_schemas(connector_name),
                    timeout=20,
                ))
            except Exception as exc:
                unavailable_connectors.add(connector_name)
                unavailable_details[connector_name] = str(exc)

    if unavailable_connectors:
        conversation.append({
            "role": "system",
            "content": (
                "The following selected connectors are unavailable and must not be called: "
                + ", ".join(
                    f"{name} ({unavailable_details.get(name, 'unknown error')})"
                    for name in sorted(unavailable_connectors)
                )
                + ". Explain that limitation if the user's request requires one of them."
            ),
        })
    conversation.append({"role": "user", "content": prompt})

    for iteration in range(MAX_AGENT_ITERATIONS):
        # Checked before every iteration, not just once before the job
        # started -- a run that had enough credits at iteration 0 can
        # burn through its whole balance by iteration 4 of a long tool
        # chain. This is what actually stops the agent "once credits are
        # finished" mid-run, not just refuses to start a new job.
        if credit_budget is not None and result.credits_used >= credit_budget:
            result.stopped_reason = "credits_exhausted"
            result.final_message = result.final_message or (
                "Stopped: this run used up the available credits before finishing. "
                f"({result.credits_used:.2f} credits used)"
            )
            break

        def record_model_usage(response, usage_tier):
            result.usage_events.append(model_usage_event(response, usage_tier))
            result.cost_usd, result.credits_used = sum_usage(result.usage_events)

        try:
            tier = await select_tier_fn(prompt, usage_callback=record_model_usage)
        except TypeError:
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

        result.usage_events.append(model_usage_event(response, tier, idempotency_key=f"iteration:{iteration}:model"))
        result.cost_usd, result.credits_used = sum_usage(result.usage_events)

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
                    query = arguments.get("query", "")
                    result.usage_events.append(search_usage_event(query))
                    tool_output = await web_search(query)
                except Exception as exc:
                    tool_output = f"error: {exc}"
            elif call_name == "browser_use":
                try:
                    arguments = call["arguments"]
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    tool_output = await browser_use(arguments.get("task", ""))
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
            elif connector_name is not None and connector_name in unavailable_connectors:
                tool_output = (
                    f"error: connector '{connector_name}' is unavailable. "
                    f"{unavailable_details.get(connector_name, 'unknown error')} "
                    "Do not retry this connector in this run."
                )
                result.stopped_reason = "connector_unavailable"
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
                        tool_result = await asyncio.wait_for(
                            connector_manager.call_tool(
                                connector_name, tool_name, arguments
                            ),
                            timeout=30,
                        )
                        tool_output = str(tool_result)
                    except Exception as exc:
                        tool_output = f"error: {exc}"
                        unavailable_connectors.add(connector_name)
                        unavailable_details[connector_name] = str(exc)
                        tools = [
                            tool for tool in tools
                            if not tool.get("function", {}).get("name", "").startswith(
                                f"{connector_name}__"
                            )
                        ]
            elif connector_name is not None:
                tool_output = f"error: connector '{connector_name}' not connected for this user"
            else:
                tool_output = f"error: unknown tool '{call_name}'"

            result.usage_events.append(usage_event(
                "tool",
                connector_name or "backend",
                tool_name,
                credits=TOOL_CALL_CREDIT_SURCHARGE,
                metadata={"iteration": iteration},
                idempotency_key=f"iteration:{iteration}:tool:{call.get('id')}",
            ))
            result.cost_usd, result.credits_used = sum_usage(result.usage_events)
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

        if result.stopped_reason == "connector_unavailable":
            result.final_message = (
                "I could not complete this request because the requested connector "
                "is unavailable."
            )
            break
    else:
        result.stopped_reason = "max_iterations_reached"

    return result.to_dict()