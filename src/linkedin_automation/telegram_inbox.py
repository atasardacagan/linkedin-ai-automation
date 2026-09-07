from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from telegram import Update
from telegram.ext import Application

from .db import SessionLocal
from .logging import safe_exception_message
from .models import TelegramUpdateInbox

log = structlog.get_logger()


async def enqueue_telegram_update(payload: dict) -> bool:
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        raise ValueError("Telegram update_id is invalid")
    async with SessionLocal() as db:
        inserted = await db.scalar(
            insert(TelegramUpdateInbox)
            .values(update_id=update_id, payload=payload)
            .on_conflict_do_nothing(index_elements=[TelegramUpdateInbox.update_id])
            .returning(TelegramUpdateInbox.update_id)
        )
        await db.commit()
    return inserted is not None


async def drain_telegram_updates(application: Application, limit: int = 10) -> None:
    for _ in range(limit):
        item = await _claim_update()
        if not item:
            return
        update_id, payload, attempt = item
        try:
            update = Update.de_json(payload, application.bot)
            failed_updates = application.bot_data.setdefault("_failed_updates", {})
            failed_updates.pop(update_id, None)
            await application.process_update(update)
            failure_type = failed_updates.pop(update_id, None)
            if failure_type:
                raise RuntimeError(f"Telegram handler failed ({failure_type})")
        except Exception as exc:
            retryable = attempt < 8
            delay = min(3600, 15 * (2 ** min(attempt, 8)))
            async with SessionLocal() as db:
                current = await db.get(TelegramUpdateInbox, update_id, with_for_update=True)
                if current:
                    current.status = "pending" if retryable else "dead"
                    current.claimed_at = None
                    current.next_attempt_at = (
                        datetime.now(UTC) + timedelta(seconds=delay) if retryable else None
                    )
                    current.completed_at = None if retryable else datetime.now(UTC)
                    current.last_error = safe_exception_message(exc, limit=300)
                    await db.commit()
            log.exception(
                "telegram_update_processing_failed",
                update_id=update_id,
                retry_scheduled=retryable,
            )
            continue
        notified_updates = application.bot_data.get("_failed_update_notified", set())
        notified_updates.discard(update_id)
        async with SessionLocal() as db:
            current = await db.get(TelegramUpdateInbox, update_id, with_for_update=True)
            if current:
                current.status = "completed"
                current.payload = {}
                current.claimed_at = None
                current.next_attempt_at = None
                current.completed_at = datetime.now(UTC)
                current.last_error = None
                await db.commit()


async def _claim_update() -> tuple[int, dict, int] | None:
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        await db.execute(
            update(TelegramUpdateInbox)
            .where(
                TelegramUpdateInbox.status == "processing",
                TelegramUpdateInbox.claimed_at < now - timedelta(minutes=15),
            )
            .values(status="pending", claimed_at=None, next_attempt_at=now)
        )
        item = (
            await db.execute(
                select(TelegramUpdateInbox)
                .where(
                    TelegramUpdateInbox.status == "pending",
                    TelegramUpdateInbox.next_attempt_at <= now,
                    TelegramUpdateInbox.attempts < 8,
                )
                .order_by(TelegramUpdateInbox.update_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if not item:
            await db.commit()
            return None
        item.status = "processing"
        item.claimed_at = now
        item.attempts += 1
        claimed = (item.update_id, dict(item.payload), item.attempts)
        await db.commit()
        return claimed
