"""
Some product features are real but are not MCP servers -- Telegram (a
personal MTProto session) and WhatsApp (a plain REST send) don't have a
url/transport/token shape to register with MCPConnectorManager. Rather
than force them into that abstraction, they're plain backend functions
exposed as OpenAI-style function tools, dispatched by name -- exactly the
pattern `web_search` already established in llm_client.py.

`connectors` passed into run_agent_loop can contain "telegram" and/or
"whatsapp" alongside real MCP connector ids; this module is what makes
those two names do something instead of silently no-oping.
"""

from __future__ import annotations

from app.connectors import telegram_service, whatsapp_service

NATIVE_CONNECTOR_IDS = frozenset({"telegram", "whatsapp"})


def get_native_tool_schemas(connectors: list[str]) -> list[dict]:
    schemas: list[dict] = []

    if "telegram" in connectors:
        schemas.append({
            "type": "function",
            "function": {
                "name": "telegram_send_message",
                "description": "Send a message on the user's own connected Telegram account to a chat, group, or user they can already reach.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat": {"type": "string", "description": "Username, phone number, or chat ID to send to."},
                        "text": {"type": "string", "description": "Message text to send."},
                    },
                    "required": ["chat", "text"],
                },
            },
        })
        schemas.append({
            "type": "function",
            "function": {
                "name": "telegram_list_chats",
                "description": "List the user's recent Telegram chats, groups, and channels (dialogs).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max number of chats to return.", "default": 20},
                    },
                },
            },
        })

    if "whatsapp" in connectors:
        schemas.append({
            "type": "function",
            "function": {
                "name": "whatsapp_send_alert",
                "description": (
                    "Send a WhatsApp alert to the user's configured recipient number. "
                    "Send-only: cannot read or receive WhatsApp messages. "
                    "Set within_24h_window=true only if you know the recipient has messaged "
                    "in the last 24 hours; otherwise a template message is sent, since Meta "
                    "rejects free-form text outside that window."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Alert text."},
                        "within_24h_window": {
                            "type": "boolean",
                            "description": "True only if the recipient has messaged within the last 24h.",
                            "default": False,
                        },
                    },
                    "required": ["message"],
                },
            },
        })

    return schemas


async def call_native_tool(user_id: str, tool_name: str, arguments: dict) -> str:
    if tool_name == "telegram_send_message":
        return await telegram_service.send_message(user_id, arguments["chat"], arguments["text"])

    if tool_name == "telegram_list_chats":
        chats = await telegram_service.list_chats(user_id, arguments.get("limit", 20))
        return str(chats)

    if tool_name == "whatsapp_send_alert":
        return await whatsapp_service.send_alert(
            user_id,
            arguments["message"],
            within_24h_window=bool(arguments.get("within_24h_window", False)),
        )

    return f"error: unknown native tool '{tool_name}'"