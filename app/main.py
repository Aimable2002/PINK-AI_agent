from fastapi import FastAPI

from app.api.routes_chat import router as chat_router

app = FastAPI()
app.include_router(chat_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
