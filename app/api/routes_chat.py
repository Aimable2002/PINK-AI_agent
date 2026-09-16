from fastapi import APIRouter, HTTPException, Depends

from app.api.schemas import (
    ChatQueuedResponse,
    ChatRequest,
    ChatStatusResponse,
)
from app.api.dependencies import get_current_user
from app.config import FREE_QUEUE_MAX_DEPTH
from app.data.supabase_client import (
    UserContext,
    get_task_by_job_id,
    update_task_by_job_id,
)
from app.queue.celery_app import celery_app, get_queue_depth
from app.queue.tasks import run_free_job, run_paid_job

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post("/chat", response_model=ChatQueuedResponse)
async def chat(payload: ChatRequest, user: UserContext = Depends(get_current_user)):
    if user.quota_exceeded:
        raise HTTPException(status_code=429, detail="Quota exceeded for current plan")

    args = [payload.prompt, payload.messages, payload.connectors, payload.mode, user.user_id]

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


@router.get("/chat/{job_id}", response_model=ChatStatusResponse)
async def chat_result(job_id: str, user: UserContext = Depends(get_current_user)):
    task = get_task_by_job_id(job_id)
    if task and task["user_id"] != user.user_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if task and task.get("status") == "cancelled":
        return {"status": "cancelled"}

    result = celery_app.AsyncResult(job_id)
    if result.state == "PENDING":
        return {"status": "pending"}
    if result.state == "FAILURE":
        return {"status": "failed", "error": str(result.result)}
    if result.state == "REVOKED":
        return {"status": "cancelled"}
    if result.state == "SUCCESS":
        if isinstance(result.result, dict) and result.result.get("stopped_reason") == "llm_call_failed":
            return {"status": "failed", "error": result.result.get("final_message")}
        return {"status": "done", "data": result.result}
    return {"status": result.state}


@router.post("/chat/{job_id}/cancel", response_model=ChatStatusResponse)
async def cancel_chat(job_id: str, user: UserContext = Depends(get_current_user)):
    task = get_task_by_job_id(job_id)
    if not task or task["user_id"] != user.user_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if task.get("status") in ("completed", "failed", "cancelled"):
        return {"status": task["status"]}

    celery_app.control.revoke(job_id, terminate=True, signal="SIGTERM")
    update_task_by_job_id(job_id, status="cancelled")
    return {"status": "cancelled"}