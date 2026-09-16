from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.dependencies import get_current_user
from app.connectors import whatsapp_service
from app.connectors.whatsapp_service import WhatsAppNotConfiguredError, WhatsAppSendError
from app.data.supabase_client import UserContext, get_client

router = APIRouter(prefix="/v1/whatsapp", tags=["whatsapp"])


class CredentialsRequest(BaseModel):
    access_token: str
    phone_number_id: str
    business_account_id: str
    alert_recipient: str


class TestSendRequest(BaseModel):
    message: str = "This is a test alert."


@router.get("/status")
async def status(user: UserContext = Depends(get_current_user)):
    client = get_client()
    resp = (
        client.table("whatsapp_credentials")
        .select("phone_number_id, alert_recipient")
        .eq("user_id", user.user_id)
        .maybe_single()
        .execute()
    )
    row = resp.data
    return {"connected": bool(row), **(row or {})}


@router.put("/credentials")
async def save_credentials(payload: CredentialsRequest, user: UserContext = Depends(get_current_user)):
    # Deliberately three separate values plus a recipient number, not one
    # bearer token against one shared URL -- that's the real WhatsApp
    # Cloud API credential shape, and forcing it into mcp_connections'
    # single-token column was the earlier mistake.
    client = get_client()
    client.table("whatsapp_credentials").upsert(
        {
            "user_id": user.user_id,
            "access_token": payload.access_token,
            "phone_number_id": payload.phone_number_id,
            "business_account_id": payload.business_account_id,
            "alert_recipient": payload.alert_recipient,
        },
        on_conflict="user_id",
    ).execute()
    return {"connected": True}


@router.delete("/credentials")
async def delete_credentials(user: UserContext = Depends(get_current_user)):
    client = get_client()
    client.table("whatsapp_credentials").delete().eq("user_id", user.user_id).execute()
    return {"connected": False}


@router.post("/test")
async def send_test(payload: TestSendRequest, user: UserContext = Depends(get_current_user)):
    try:
        result = await whatsapp_service.send_alert(user.user_id, payload.message, within_24h_window=False)
    except WhatsAppNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WhatsAppSendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"result": result}