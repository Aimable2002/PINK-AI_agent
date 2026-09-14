from fastapi import HTTPException, Header

from app.data.supabase_client import (
    UserContext,
    verify_token_and_get_user,
    get_user_context,
)


async def get_current_user(authorization: str = Header(...)) -> UserContext:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed bearer token")
    token = authorization.removeprefix("Bearer ")
    try:
        user_id = await verify_token_and_get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return await get_user_context(user_id)
