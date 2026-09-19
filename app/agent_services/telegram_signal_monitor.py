"""
Telegram Signal Monitor -- the first real agent service.

Entry points (called by app/queue/agent_tasks.py, not directly by the
daemon -- see that file for why the daemon only does the cheap filter
and enqueues a Celery task for everything past it):

  looks_like_trading_text(text) -> bool
      Layer-1 cheap filter. Runs inline in the daemon's event handler,
      before anything reaches a queue. Deliberately dumb and fast.

  async def score_signal(user_id, service_row, channel, raw_text) -> dict
      The real (LLM) step normalizes the original message into a stable
      Forex or binary-option JSON signal. The raw message is retained for
      audit, but downstream code uses the normalized object.
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
    "alert_chat": "me",       # where the alert is sent -- 'me' = Saved Messages
}


_TICKER_PATTERN = re.compile(
    r"\$[A-Z]{2,6}\b|"
    r"\b[A-Z]{3,4}/[A-Z]{3,4}\b|"
    r"\b(XAU|XAG|BTC|ETH|EUR|GBP|USD|JPY|CHF|CAD|AUD|NZD)(USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD)\b",
    re.IGNORECASE,
)
_ENTRY_LABEL = re.compile(r"\bentry\b", re.IGNORECASE)
_AT_LABEL = re.compile(r"\b(buy|sell|long|short)\b[^.\n]{0,25}?\bat\b", re.IGNORECASE)
_TP_LABEL = re.compile(r"\btp\s?\d{0,2}\b|\btake\s?profit\b", re.IGNORECASE)
_SL_LABEL = re.compile(r"\bsl\b|\bstop\s?loss\b", re.IGNORECASE)
# Up to 12 filler characters (e.g. " Price: ", ": ", " now at ", " LIMIT
# at ") between a label and the number that belongs to it, then the
# number itself. Kept in its own group with the sign, if any, still
# attached, so the caller can tell a bare price (1.1467) apart from a
# signed pip/point delta (+20, -60).
_NUMBER_AFTER = re.compile(r"[^\d\n]{0,12}([+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[+-]?\.\d+)")
_PIP_SUFFIX = re.compile(r"^\s*(pips?|points?|pts?)\b", re.IGNORECASE)


def _price_right_after(text: str, pos: int) -> bool:
    """
    True only if a genuine price level (not a signed pips/points delta)
    immediately follows position `pos` in `text`, within a short amount
    of filler text.
    """
    m = _NUMBER_AFTER.match(text, pos)
    if not m:
        return False
    number = m.group(1)
    if number.startswith("+") or number.startswith("-"):
        return False  # "+20", "-60" -- an outcome, not a price level
    if _PIP_SUFFIX.match(text, m.end()):
        return False  # "20 pips" -- an outcome stated without a sign
    return True


def _has_price_for(label_pattern: re.Pattern, text: str) -> bool:
    return any(_price_right_after(text, m.end()) for m in label_pattern.finditer(text))


def looks_like_trading_text(text: str) -> bool:
    """
    Layer-1 filter: a real currency-pair ticker, an entry price (the
    number attached to the word "entry", or to "buy/sell/long/short ...
    at"), a TP price, and an SL price -- each of those last three must be
    an actual price level immediately after its label, not just the
    label's word appearing somewhere in the text. This is what actually
    distinguishes a genuine entry signal ("TP1 1.1467", "SL 4310") from a
    result/recap post ("TP +20 pips", "SL -60pips") or a bare mention of
    the word with nothing concrete attached -- not message length or how
    many times a word repeats, both of which are incidental. This is the
    gate that decides what reaches the LLM (and costs credits) and what
    shows up in Recent Signals at all, so precision matters far more than
    recall here.
    """
    if not text or len(text.strip()) < 3:
        return False
    has_ticker = bool(_TICKER_PATTERN.search(text))
    has_entry_price = _has_price_for(_ENTRY_LABEL, text) or _has_price_for(_AT_LABEL, text)
    has_tp_price = _has_price_for(_TP_LABEL, text)
    has_sl_price = _has_price_for(_SL_LABEL, text)
    # This is only a cheap candidate gate. The model extracts levels when
    # the message format is unusual.
    return has_ticker and bool(_ENTRY_LABEL.search(text) or _AT_LABEL.search(text)) and bool(
        _TP_LABEL.search(text) or _SL_LABEL.search(text)
    )


_SCORING_INSTRUCTIONS = """Extract a trading signal from this Telegram message. Do not score confidence \
and do not invent missing values. Return ONLY one JSON object with this exact shape:
{{"is_signal": true|false, "signal_type": "forex"|"binary_option", "symbol": "...", \
"direction": "buy"|"sell"|"call"|"put", "entry": number|null, \
"take_profits": [number], "stop_loss": number|null, "expiry_minutes": integer|null, \
"reasoning": "short explanation", "parse_status": "parsed"|"rejected"}}

Use an empty take_profits array when none are present. For binary options, expiry_minutes may be null \
only when absent. Never fabricate levels.

Message:
---
{message}
---
"""


def _parse_signal_response(text: str) -> dict:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
        signal_type = str(data.get("signal_type", "forex")).lower()
        direction = str(data.get("direction", "")).lower()
        take_profits = [float(value) for value in (data.get("take_profits") or [])]
        valid = (
            bool(data.get("is_signal"))
            and signal_type in {"forex", "binary_option"}
            and direction in {"buy", "sell", "call", "put"}
            and bool(data.get("symbol"))
            and data.get("entry") is not None
            and (signal_type == "binary_option" or bool(take_profits) or data.get("stop_loss") is not None)
        )
        return {
            "is_signal": valid,
            "signal_type": signal_type,
            "symbol": str(data.get("symbol") or "").upper(),
            "direction": direction,
            "entry": float(data["entry"]) if data.get("entry") is not None else None,
            "take_profits": take_profits,
            "stop_loss": float(data["stop_loss"]) if data.get("stop_loss") is not None else None,
            "expiry_minutes": int(data["expiry_minutes"]) if data.get("expiry_minutes") is not None else None,
            "reasoning": str(data.get("reasoning", ""))[:500],
            "parse_status": "parsed" if valid else "rejected",
        }
    except Exception:
        return {"is_signal": False, "parse_status": "rejected", "reasoning": "Model response was not valid signal JSON."}


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

    signal = _parse_signal_response(result.get("final_message", ""))
    update_signal(signal_row["id"], **{
        "signal_type": signal.get("signal_type"),
        "symbol": signal.get("symbol"),
        "direction": signal.get("direction"),
        "entry": signal.get("entry"),
        "take_profits": signal.get("take_profits", []),
        "stop_loss": signal.get("stop_loss"),
        "expiry_minutes": signal.get("expiry_minutes"),
        "normalized_signal": signal,
        "model_reasoning": signal.get("reasoning", ""),
        "parse_status": signal.get("parse_status", "rejected"),
    })
    touch_service_last_run(service_row["id"])

    return {
        "signal_id": signal_row["id"],
        **signal,
        "should_alert": signal.get("is_signal", False),
        "raw_text": raw_text,
        "channel": channel,
        "alert_chat": service_row.get("config", {}).get("alert_chat", DEFAULT_CONFIG["alert_chat"]),
    }


def format_alert(channel: str | None, signal: dict) -> str:
    header = f"Signal from {channel}" if channel else "Signal"
    return f"{header}\n\n{json.dumps(signal, ensure_ascii=True, indent=2)}"