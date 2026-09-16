from pydantic import BaseModel, Field
from typing import Literal


class ChatRequest(BaseModel):
    prompt: str
    messages: list[dict]
    connectors: list[str] = []
    mode: Literal["chat", "agent"] = "chat"


class AgentStepResponse(BaseModel):
    connector: str
    action: str
    detail: str | None = None


class AgentResultResponse(BaseModel):
    final_message: str | None = None
    steps: list[AgentStepResponse] = Field(default_factory=list)
    stopped_reason: str | None = None
    tiers_used: list[str] = Field(default_factory=list)
    tier: str = "medium"


class ChatQueuedResponse(BaseModel):
    job_id: str
    status: Literal["queued"]
    plan: str


class ChatStatusResponse(BaseModel):
    status: str
    data: AgentResultResponse | None = None
    error: str | None = None
