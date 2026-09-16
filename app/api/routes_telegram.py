from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.dependencies import get_current_user
from app.connectors import telegram_service
from app.connectors.telegram_service import TelegramConfigError, TelegramLoginError
from app.data.supabase_client import UserContext

router = APIRouter(prefix="/v1/telegram", tags=["telegram"])


class StartRequest(BaseModel):
    phone: str


class CodeRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str


@router.get("/status")
async def status(user: UserContext = Depends(get_current_user)):
    return await telegram_service.get_status(user.user_id)


@router.post("/start")
async def start(payload: StartRequest, user: UserContext = Depends(get_current_user)):
    try:
        return await telegram_service.start_login(user.user_id, payload.phone)
    except TelegramConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TelegramLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/verify")
async def verify(payload: CodeRequest, user: UserContext = Depends(get_current_user)):
    try:
        return await telegram_service.submit_code(user.user_id, payload.code)
    except TelegramLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/2fa")
async def two_fa(payload: PasswordRequest, user: UserContext = Depends(get_current_user)):
    try:
        return await telegram_service.submit_password(user.user_id, payload.password)
    except TelegramLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("")
async def disconnect(user: UserContext = Depends(get_current_user)):
    await telegram_service.disconnect(user.user_id)
    return {"connected": False}