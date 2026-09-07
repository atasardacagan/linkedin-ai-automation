import json
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .logging import configure_logging
from .scheduler import scheduler, start_scheduler
from .telegram import build_application
from .telegram_inbox import drain_telegram_updates, enqueue_telegram_update

configure_logging(settings.log_level)
telegram_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    if settings.telegram_bot_token.get_secret_value():
        telegram_app = build_application()
        await telegram_app.initialize()
        await telegram_app.start()
        if settings.public_base_url.startswith("https://"):
            await telegram_app.bot.set_webhook(
                url=f"{settings.public_base_url.rstrip('/')}/webhooks/telegram",
                secret_token=settings.telegram_webhook_secret.get_secret_value(),
                allowed_updates=["message", "callback_query"],
            )
        scheduler.add_job(
            drain_telegram_updates,
            "interval",
            seconds=2,
            args=[telegram_app],
            id="telegram_inbox",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    start_scheduler()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(
    title="LinkedIn AI Automation",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if settings.app_env == "production" else "/docs",
)


def _is_admin_payload(payload: dict) -> bool:
    message = payload.get("message")
    callback = payload.get("callback_query")
    if isinstance(message, dict):
        sender = message.get("from")
        chat = message.get("chat")
    elif isinstance(callback, dict):
        sender = callback.get("from")
        callback_message = callback.get("message")
        chat = callback_message.get("chat") if isinstance(callback_message, dict) else None
    else:
        return False
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return False
    sender_id = sender.get("id")
    chat_id = chat.get("id")
    return bool(
        isinstance(sender_id, int)
        and not isinstance(sender_id, bool)
        and isinstance(chat_id, int)
        and not isinstance(chat_id, bool)
        and chat.get("type") == "private"
        and sender_id == settings.telegram_admin_user_id
        and chat_id == settings.telegram_admin_chat_id
    )


@app.get("/health")
async def health(db: Annotated[AsyncSession, Depends(get_session)]):
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "dry_run": settings.dry_run}


@app.post("/webhooks/telegram", include_in_schema=False)
async def telegram_webhook(
    request: Request, x_telegram_bot_api_secret_token: str | None = Header(None)
):
    if not telegram_app:
        raise HTTPException(503, "Telegram is not configured")
    expected = settings.telegram_webhook_secret.get_secret_value()
    supplied = x_telegram_bot_api_secret_token or ""
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(403, "Invalid webhook secret")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "Webhook payload too large")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Telegram update must be a JSON object")
    if not _is_admin_payload(payload):
        return {"ok": True, "queued": False}
    try:
        queued = await enqueue_telegram_update(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "queued": queued}
