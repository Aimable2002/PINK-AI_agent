from pydantic import BaseModel


class ChatRequest(BaseModel):
    prompt: str
    messages: list[dict]
    connectors: list[str] = []
