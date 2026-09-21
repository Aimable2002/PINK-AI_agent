from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import CREDITS_PER_USD


@dataclass
class UsageEvent:
    operation_type: str
    provider: str
    resource: str
    cost_usd: float = 0.0
    credits: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    units: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.credits is None:
            self.credits = self.cost_usd * CREDITS_PER_USD
        self.cost_usd = round(max(0.0, float(self.cost_usd)), 6)
        self.credits = round(max(0.0, float(self.credits)), 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type,
            "provider": self.provider,
            "resource": self.resource,
            "cost_usd": self.cost_usd,
            "credits": self.credits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "units": self.units,
            "metadata": self.metadata,
            "idempotency_key": self.idempotency_key,
        }


def usage_event(
    operation_type: str,
    provider: str,
    resource: str,
    *,
    cost_usd: float = 0.0,
    credits: float | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    units: float = 1.0,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return UsageEvent(
        operation_type=operation_type,
        provider=provider,
        resource=resource,
        cost_usd=cost_usd,
        credits=credits,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        units=units,
        metadata=metadata or {},
        idempotency_key=idempotency_key,
    ).to_dict()


def sum_usage(events: list[dict[str, Any]]) -> tuple[float, float]:
    return (
        round(sum(float(event.get("cost_usd", 0.0) or 0.0) for event in events), 6),
        round(sum(float(event.get("credits", 0.0) or 0.0) for event in events), 6),
    )
