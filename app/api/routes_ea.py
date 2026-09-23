from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.dependencies import get_current_user
from app.data.supabase_client import (
    UserContext,
    claim_trade_order,
    create_trade_execution,
    list_pending_trade_orders,
)

router = APIRouter(prefix="/v1/ea", tags=["ea"])


class ExecutionReceipt(BaseModel):
    mt5_account_id: str = Field(min_length=1, max_length=100)
    broker_ticket: str | None = Field(default=None, max_length=100)
    status: str
    requested_price: float | None = None
    fill_price: float | None = None
    volume: float | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=1000)
    raw_response: dict = Field(default_factory=dict)


@router.get("/orders")
async def pending_orders(
    limit: int = Query(default=50, ge=1, le=200),
    client_id: str | None = Query(default=None, max_length=200),
    user: UserContext = Depends(get_current_user),
):
    return {"orders": list_pending_trade_orders(user.user_id, limit, client_id)}


@router.post("/orders/{order_id}/claim")
async def claim_order(
    order_id: str,
    client_id: str = Query(min_length=1, max_length=200),
    user: UserContext = Depends(get_current_user),
):
    order = claim_trade_order(order_id, user.user_id, client_id)
    if not order:
        raise HTTPException(status_code=409, detail="Order is unavailable, expired, or already claimed.")
    return {"order": order}


@router.post("/orders/{order_id}/execution")
async def report_execution(
    order_id: str,
    payload: ExecutionReceipt,
    user: UserContext = Depends(get_current_user),
):
    if payload.status not in {"executed", "rejected", "failed", "partial"}:
        raise HTTPException(status_code=422, detail="Unsupported execution status.")
    try:
        execution = create_trade_execution(order_id, user.user_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"execution": execution}