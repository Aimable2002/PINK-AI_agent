from fastapi import APIRouter, HTTPException, Depends

from app.api.schemas import ChatRequest
from app.api.dependencies import get_current_user
from app.config import FREE_QUEUE_MAX_DEPTH
from app.data.supabase_client import UserContext
from app.queue.celery_app import celery_app, get_queue_depth
from app.queue.tasks import run_free_job, run_paid_job

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post("/chat")
async def chat(payload: ChatRequest, user: UserContext = Depends(get_current_user)):
    if user.quota_exceeded:
        raise HTTPException(status_code=429, detail="Quota exceeded for current plan")

    args = [payload.prompt, payload.messages, payload.connectors]

    if user.plan == "paid":
        job = run_paid_job.apply_async(args=args, queue="paid_priority")
    else:
        depth = get_queue_depth("free_standard")
        if depth >= FREE_QUEUE_MAX_DEPTH:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Free tier is at capacity ({depth} jobs queued). "
                    "Please try again shortly, or upgrade to the paid tier "
                    "for priority processing."
                ),
            )
        job = run_free_job.apply_async(args=args, queue="free_standard")

    return {"job_id": job.id, "status": "queued", "plan": user.plan}


@router.get("/chat/{job_id}")
async def chat_result(job_id: str, user: UserContext = Depends(get_current_user)):
    result = celery_app.AsyncResult(job_id)
    if result.state == "PENDING":
        return {"status": "pending"}
    if result.state == "FAILURE":
        return {"status": "failed", "error": str(result.result)}
    if result.state == "SUCCESS":
        return {"status": "done", "data": result.result}
    return {"status": result.state}