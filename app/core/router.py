"""
Tier routing via LLM-as-classifier: a cheap model call (its own
"classifier" tier, configured in config.py alongside small/medium/best
and equally MODE-aware) rates each incoming prompt's difficulty, and that score cuts
into three tiers. This replaces the earlier RouteLLM-based design.

Why this instead of RouteLLM's trained `mf` classifier: RouteLLM's
router isn't a chat model -- it needs its own downloaded checkpoint (HF)
plus, per its own docs, a live OpenAI API call for embeddings regardless
of which models you're routing between. That means RouteLLM would be the
one piece of this entire system needing dedicated hosting/disk and a
hard dependency on OpenAI specifically. LLM-as-classifier needs none of
that -- it's just another OpenRouter/RunPod call through the exact same
MODE-aware call_tier() every other tier already uses. Nothing new to
host, nowhere new to deploy. This is a documented pattern (e.g. NVIDIA's
Switchyard LLM Classifier Routing), not a hand-rolled substitute.
"""

import re

from app.config import TIER_THRESHOLDS
from app.core.llm_client import call_tier

_CLASSIFIER_TIER = "classifier"  # its own MODE-aware config entry, not borrowed from "small"

_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a difficulty classifier for a trading-strategy AI agent. "
    "Given the user's request, output ONLY a single number between 0.0 "
    "and 1.0 representing how difficult/complex the request is to "
    "fulfill (0.0 = trivial, e.g. a greeting or simple lookup; 1.0 = "
    "very hard, e.g. designing and iterating a novel trading strategy). "
    "Output the number only, nothing else."
)

_NUMBER_PATTERN = re.compile(r"(?:0(?:\.\d+)?|1(?:\.0+)?)")


def _parse_score(raw_text: str | None) -> float | None:
    if not raw_text:
        return None
    match = _NUMBER_PATTERN.search(raw_text)
    if not match:
        return None
    try:
        value = float(match.group())
    except ValueError:
        return None
    return max(0.0, min(1.0, value))


async def score_query(prompt: str, call_tier_fn=call_tier) -> float:
    """
    Calls the classifier tier with a scoring instruction. Returns a
    difficulty score in [0.0, 1.0]. Falls back to a safe default (0.5,
    i.e. medium) if the classifier response can't be parsed -- this is
    logged as a fallback rather than silently treated as a real score,
    and the fallback is a config constant so it can be tuned/audited.
    """
    messages = [
        {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    response = await call_tier_fn(_CLASSIFIER_TIER, messages)

    try:
        raw_text = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError):
        raw_text = None

    score = _parse_score(raw_text)
    if score is None:
        return 0.5  # unparseable classifier output -> safe middle default
    return score


async def select_tier(prompt: str, call_tier_fn=call_tier) -> str:
    score = await score_query(prompt, call_tier_fn=call_tier_fn)
    if score < TIER_THRESHOLDS["small_to_medium"]:
        return "small"
    elif score < TIER_THRESHOLDS["medium_to_best"]:
        return "medium"
    return "best"
