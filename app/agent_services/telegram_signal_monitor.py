"""
Telegram Signal Monitor -- the first real agent service.

Entry points (called by app/queue/agent_tasks.py, not directly by the
daemon -- see that file for why the daemon only does the cheap filter
and enqueues a Celery task for everything past it):

  looks_like_trading_text(text) -> bool
      Layer-1 cheap filter. Runs inline in the daemon's event handler,
      before anything reaches a queue. Deliberately dumb and fast.

  async def score_signal(user_id, service_row, channel, raw_text) -> dict
      The real (LLM) step. Per product decision: this does NOT rewrite,
      summarise, or re-analyse the message, and does NOT pull separate
      market/chart data. Its only job is judging whether the raw text,
      as given, is a genuine trading signal, using the web_search tool
      only if it needs outside context to judge plausibility -- and
      returning a confidence score. The alert that goes out to the user
      is the ORIGINAL text plus that score, never a rewritten version.
"""

from __future__ import annotations

import json
import re

from app.agent_services.base import AgentServicePaused
from app.connectors.manager import MCPConnectorManager
from app.core.agent_runtime import run_agent_loop
from app.data.supabase_client import set_service_status, touch_service_last_run, update_signal

SERVICE_ID = "telegram-signal-monitor"

DEFAULT_CONFIG = {
    "monitored_chats": [],   # list of Telegram chat ids/usernames to watch
    "min_confidence": 6,      # 0-10; below this, scored but not alerted
    "alert_chat": "me",       # where the alert is sent -- 'me' = Saved Messages
}


_TICKER_PATTERN = re.compile(
    r"\$[A-Z]{2,6}\b|"
    r"\b[A-Z]{3,4}/[A-Z]{3,4}\b|"
    r"\b(XAU|XAG|BTC|ETH|EUR|GBP|USD|JPY|CHF|CAD|AUD|NZD)(USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD)\b",
    re.IGNORECASE,
)
_DIRECTION_PATTERN = re.compile(r"\b(buy|sell|long|short)\b", re.IGNORECASE)
_ENTRY_PATTERN = re.compile(r"\bentry\b", re.IGNORECASE)
_TP_PATTERN = re.compile(r"\btp\s?\d?\b|\btake\s?profit\b", re.IGNORECASE)
_SL_PATTERN = re.compile(r"\bsl\b|\bstop\s?loss\b", re.IGNORECASE)
# A price-looking number: a decimal (1.1487), a comma-grouped thousand
# (2,150.50), or a bare 3+-digit integer (4392) -- not a lone 1-2 digit
# number like the "2" in "GET READY FOR 2 SIGNALS", which used to be
# enough on its own to count as a "price".
_PRICE_PATTERN = re.compile(r"\b\d{1,3}(,\d{3})+(\.\d+)?\b|\b\d*\.\d+\b|\b\d{3,}\b")


def looks_like_trading_text(text: str) -> bool:
    """
    Layer-1 filter: by design, this rejects almost everything. Only a
    complete structured entry -- a real currency-pair ticker, an explicit
    direction (or the word "entry"), at least one TP, an SL, and an
    actual price-looking number somewhere in the message -- passes.
    Update/close-only posts (e.g. "TP3 DONE 170+ PIPS", no SL) and
    anything less than the full structure are deliberately excluded, not
    just engagement-bait/hype text. This is the gate that decides what
    reaches the LLM (and costs credits) and what shows up in Recent
    Signals at all, so precision matters far more than recall here.
    """
    if not text or len(text.strip()) < 3:
        return False
    has_ticker = bool(_TICKER_PATTERN.search(text))
    has_direction = bool(_DIRECTION_PATTERN.search(text)) or bool(_ENTRY_PATTERN.search(text))
    has_tp = bool(_TP_PATTERN.search(text))
    has_sl = bool(_SL_PATTERN.search(text))
    has_price = bool(_PRICE_PATTERN.search(text))
    return has_ticker and has_direction and has_tp and has_sl and has_price


_SCORING_INSTRUCTIONS = """You are judging whether a message forwarded from Telegram is a genuine, \
actionable trading signal -- not analysing the trade, not restating or rewriting the message, \
just judging how credible/genuine it looks as a signal. You may use the web_search tool if you \
need outside context to judge plausibility (e.g. checking whether an asset name is real, whether \
a claimed event actually happened), but you are expected to fetch live price/chart data.

Respond with ONLY a single JSON object, no other text, no markdown fences:
{{"is_signal": true|false, "confidence": <integer 0-10>, "reasoning": "<one short sentence>"}}

Message:
---
{message}
---
"""


def _parse_scoring_response(text: str, fell_back: bool) -> tuple[bool, int, str]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
        is_signal = bool(data.get("is_signal", True))
        confidence = int(max(0, min(10, round(float(data.get("confidence", 5))))))
        reasoning = str(data.get("reasoning", ""))[:500]
        return is_signal, confidence, reasoning
    except Exception:
        match = re.search(r"\b([0-9]|10)\b", cleaned)
        confidence = int(match.group(1)) if match else 5
        return True, confidence, f"(unparsed model response, fell back to confidence={confidence})"


async def score_signal(user_id: str, service_row: dict, channel: str | None, raw_text: str, signal_row: dict) -> dict:
    """
    The full scoring step for one message that already passed the Layer-1
    filter. `signal_row` is created once by the caller (score_signal_job),
    not here -- Celery retries this whole coroutine on failure, and
    inserting a fresh row on every attempt used to leave duplicate rows
    for a single incoming message every time scoring failed and retried.
    Returns a dict describing what happened (for logging by the caller);
    raises AgentServicePaused if the user is out of credits, after
    already pausing the service and recording why.
    """
    from app.queue.tasks import charge_usage, get_remaining_credits

    credit_budget = await get_remaining_credits(user_id)
    if credit_budget is not None and credit_budget <= 0:
        set_service_status(service_row["id"], "paused", paused_reason="credits_exhausted")
        update_signal(signal_row["id"], alert_error="skipped: credits exhausted")
        raise AgentServicePaused("credits_exhausted")

    prompt = _SCORING_INSTRUCTIONS.format(message=raw_text)
    result = await run_agent_loop(
        prompt,
        messages=[],
        connectors=[], 
        connector_manager=MCPConnectorManager(),
        mode="agent",
        user_id=user_id,
        credit_budget=credit_budget,
    )
    charge_usage(user_id, result)

    fell_back = result.get("stopped_reason") not in (None, "final_answer")
    is_signal, confidence, reasoning = _parse_scoring_response(result.get("final_message", ""), fell_back)

    update_signal(signal_row["id"], confidence_score=confidence, model_reasoning=reasoning)
    touch_service_last_run(service_row["id"])

    min_confidence = int(service_row.get("config", {}).get("min_confidence", DEFAULT_CONFIG["min_confidence"]))
    should_alert = is_signal and confidence >= min_confidence

    return {
        "signal_id": signal_row["id"],
        "is_signal": is_signal,
        "confidence": confidence,
        "reasoning": reasoning,
        "should_alert": should_alert,
        "raw_text": raw_text,
        "channel": channel,
        "alert_chat": service_row.get("config", {}).get("alert_chat", DEFAULT_CONFIG["alert_chat"]),
    }


def format_alert(channel: str | None, raw_text: str, confidence: int) -> str:
    """
    Per product decision: don't reformat or summarise the original
    message -- pass it through as-is, add only the confidence score and
    enough context (source channel) to know what's being looked at.
    """
    header = f"\U0001F4CA Signal from {channel}" if channel else "\U0001F4CA Signal"
    return f"{header}\n\n{raw_text}\n\nConfidence: {confidence}/10"