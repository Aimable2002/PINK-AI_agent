from fastapi import FastAPI

from app.api.routes_chat import router as chat_router
from app.api.routes_telegram import router as telegram_router
from app.api.routes_whatsapp import router as whatsapp_router

app = FastAPI()
app.include_router(chat_router)
app.include_router(telegram_router)
app.include_router(whatsapp_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}