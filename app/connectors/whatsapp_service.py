"""
WhatsApp is not an MCP server either -- it's a plain REST call to Meta's
Graph API. There's nothing to "connect a session" to; each user just needs
their own WhatsApp Business Cloud API credentials (a System User access
token, their Phone Number ID, and their WhatsApp Business Account ID),
generated once in their own Meta Business Suite and pasted in.

This is deliberately send-only, per the product scope: no webhook, no
inbound handling, no messages.read.

The 24-hour rule is a real Meta constraint, not optional: a template
message is required for the first message to a recipient, or any message
sent more than 24 hours after that recipient last messaged the business
number. Free-form text outside that window is rejected by the API. Since
this product only ever sends outbound alerts (the recipient's own phone
never messages back into this flow), every call here is effectively
"cold" from Meta's perspective unless the caller explicitly says
otherwise -- so the default is the template path, with free-form
available for callers who know they're inside an active window.
"""

from __future__ import annotations

import httpx

from app.config import (
    WHATSAPP_DEFAULT_TEMPLATE_LANG,
    WHATSAPP_DEFAULT_TEMPLATE_NAME,
    WHATSAPP_GRAPH_API_VERSION,
)
from app.data.supabase_client import get_whatsapp_credentials_row


class WhatsAppNotConfiguredError(RuntimeError):
    pass


class WhatsAppSendError(RuntimeError):
    pass


async def _get_credentials(user_id: str) -> dict:
    row = await get_whatsapp_credentials_row(user_id)
    if not row or not row.get("access_token") or not row.get("phone_number_id"):
        raise WhatsAppNotConfiguredError(
            "WhatsApp is not connected for this user (missing access token or phone number id)."
        )
    return row


async def send_alert(
    user_id: str,
    message: str,
    *,
    within_24h_window: bool = False,
    template_name: str | None = None,
    template_lang: str | None = None,
) -> str:
    """
    Send an alert to the user's configured recipient number.

    within_24h_window=True sends free-form text (only valid if the
    recipient has messaged this business number in the last 24h -- the
    caller is responsible for knowing that, this function doesn't track
    conversation state). Otherwise a template is sent, since Meta will
    reject free-form text to a cold/expired-window recipient.
    """
    creds = await _get_credentials(user_id)
    access_token = creds["access_token"]
    phone_number_id = creds["phone_number_id"]
    recipient = creds.get("alert_recipient")
    if not recipient:
        raise WhatsAppNotConfiguredError("No alert recipient number configured for this user.")

    url = f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    if within_24h_window:
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": message},
        }
    else:
        # Free-form text isn't allowed here -- fall back to the approved
        # template. The template body can't carry arbitrary alert text
        # unless the account has an approved template with a body
        # variable for it; without one, the default template (e.g. the
        # sandbox "hello_world") is sent as a heads-up, and the real
        # content should follow once the recipient replies and opens a
        # 24h window, or once a custom approved template exists for this.
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": template_name or WHATSAPP_DEFAULT_TEMPLATE_NAME,
                "language": {"code": template_lang or WHATSAPP_DEFAULT_TEMPLATE_LANG},
            },
        }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=payload)

    if response.status_code >= 400:
        raise WhatsAppSendError(f"WhatsApp API error ({response.status_code}): {response.text}")

    return "Alert sent." if within_24h_window else "Template alert sent (cold/first contact or expired 24h window)."