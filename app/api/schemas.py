from pydantic import BaseModel
from typing import Literal


class ChatRequest(BaseModel):
    prompt: str
    messages: list[dict]
    connectors: list[str] = []
    mode: Literal["chat", "agent"] = "chat"
