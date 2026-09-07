import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from telegram import Bot

from .config import settings
from .db import SessionLocal
from .logging import safe_exception_message
from .models import NotificationOutbox, Post

log = structlog.get_logger()


async def deliver_notification(post_id: uuid.UUID | None = None, bot: Bot | None = None) -> bool:
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        await db.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.status == "processing",
                NotificationOutbox.claimed_at < now - timedelta(minutes=10),
            )
            .values(status="pending", claimed_at=None)
        )
        query = (
            select(NotificationOutbox)
            .where(
                NotificationOutbox.status == "pending",
                NotificationOutbox.next_attempt_at <= now,
                NotificationOutbox.attempts < 8,
            )
            .order_by(NotificationOutbox.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if post_id:
            query = query.where(NotificationOutbox.post_id == post_id)
        item = (await db.execute(query)).scalar_one_or_none()
        if not item:
            await db.commit()
            return False
        item.status = "processing"
        item.claimed_at = now
        item.attempts += 1
        item_id = item.id
        selected_post_id = item.post_id
        await db.commit()

    async with SessionLocal() as db:
        post = (await db.execute(select(Post).where(Post.id == selected_post_id))).scalar_one()

    owned_bot = bot is None
    active_bot = bot or Bot(settings.telegram_bot_token.get_secret_value())
    try:
        if owned_bot:
            await active_bot.initialize()
        if item.kind == "draft_ready":
            # Delayed import avoids a module cycle with command handlers.
            from .telegram import send_draft

            await send_draft(active_bot, post)
        elif item.kind == "publish_succeeded":
            await active_bot.send_message(
                settings.telegram_admin_chat_id,
                f"✅ LinkedIn’de yayınlandı\n{post.linkedin_url}",
            )
        elif item.kind == "dry_run_completed":
            await active_bot.send_message(
                settings.telegram_admin_chat_id,
                "🧪 Dry-run başarıyla tamamlandı; LinkedIn’e hiçbir veri gönderilmedi. "
                f"Kayıt: {str(post.id)[:8]}",
            )
        elif item.kind == "publish_reconciliation_required":
            await active_bot.send_message(
                settings.telegram_admin_chat_id,
                "⚠️ LinkedIn yanıtı belirsiz. Otomatik tekrar durduruldu; profilinizi kontrol "
                f"edin.\nKayıt UUID: {post.id}\n"
                "Ardından /reconcile komutunu kullanın; ayrıntı için /help yazın.",
            )
        elif item.kind == "publish_retry_scheduled":
            await active_bot.send_message(
                settings.telegram_admin_chat_id,
                f"⚠️ LinkedIn geçici hatası. Güvenli tekrar planlandı. Kayıt: {str(post.id)[:8]}",
            )
        elif item.kind == "publish_integrity_failure":
            await active_bot.send_message(
                settings.telegram_admin_chat_id,
                "❌ Onaylanmış metin veya görsel değişmiş/kaybolmuş görünüyor. Yayın kalıcı "
                f"olarak durduruldu; yeni onay gerekir. Kayıt: {str(post.id)[:8]}",
            )
        else:
            await active_bot.send_message(
                settings.telegram_admin_chat_id,
                f"❌ LinkedIn yayını reddedildi; otomatik tekrar yok. Kayıt: {str(post.id)[:8]}",
            )
    except Exception as exc:
        delay = min(3600, 2 ** min(item.attempts, 10) * 15)
        retryable = item.attempts < 8
        async with SessionLocal() as db:
            current = await db.get(NotificationOutbox, item_id, with_for_update=True)
            current.status = "pending" if retryable else "dead"
            current.claimed_at = None
            current.next_attempt_at = (
                datetime.now(UTC) + timedelta(seconds=delay) if retryable else None
            )
            current.last_error = safe_exception_message(exc, limit=300)
            await db.commit()
        log.exception("telegram_delivery_failed", post_id=str(selected_post_id))
        return False
    finally:
        if owned_bot:
            await active_bot.shutdown()

    async with SessionLocal() as db:
        current = await db.get(NotificationOutbox, item_id, with_for_update=True)
        current.status = "sent"
        current.claimed_at = None
        current.sent_at = datetime.now(UTC)
        current.last_error = None
        await db.commit()
    return True


async def drain_notifications(limit: int = 10) -> None:
    for _ in range(limit):
        if not await deliver_notification():
            break
