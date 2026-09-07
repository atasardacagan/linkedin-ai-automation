import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import dateparser
import structlog
from sqlalchemy import delete, func, select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .ai import ContentEngine
from .analytics import calculate_engagement_rate
from .config import settings
from .db import SessionLocal
from .models import (
    AnalyticsSnapshot,
    NotificationOutbox,
    Post,
    PostStatus,
    Setting,
    TelegramPendingAction,
    Topic,
)
from .service import (
    _content_addressed_image,
    approve,
    audit,
    canonical_commentary,
    create_draft,
    publish_approved,
    reconcile_publication,
    reject,
    revise,
    revise_image,
    schedule_approved,
)

log = structlog.get_logger()
CALENDAR_DEFAULTS = {"weekdays": [0, 2, 4], "hour": 9, "minute": 0}
WEEKDAY_LABELS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
CONFIRMATION_REQUIRED = {
    "remove_topics",
    "pause",
    "resume",
    "set_frequency",
    "set_days",
    "set_time",
    "skip_until",
}
ACTION_LABELS = {
    "remove_topics": "konuları kaldırma",
    "pause": "otomatik üretimi durdurma",
    "resume": "otomatik üretimi yeniden başlatma",
    "set_frequency": "üretim sıklığını değiştirme",
    "set_days": "üretim günlerini değiştirme",
    "set_time": "üretim saatini değiştirme",
    "skip_until": "belirtilen tarihe kadar üretimi atlama",
}


def is_admin(update: Update) -> bool:
    return bool(
        update.effective_chat
        and update.effective_user
        and update.effective_chat.type == "private"
        and update.effective_chat.id == settings.telegram_admin_chat_id
        and update.effective_user.id == settings.telegram_admin_user_id
    )


def _secured_callback(action: str, post: Post) -> str:
    value = f"{action}:{post.id}:{post.approval_nonce}"
    if len(value.encode()) > 64:
        raise ValueError("Telegram callback data exceeds 64 bytes")
    return value


def review_keyboard(post: Post) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ ONAYLA", callback_data=_secured_callback("a", post)),
                InlineKeyboardButton("✏️ REVİZE ET", callback_data=_secured_callback("r", post)),
            ],
            [
                InlineKeyboardButton("🔄 YENİDEN ÜRET", callback_data=_secured_callback("g", post)),
                InlineKeyboardButton(
                    "🖼 GÖRSELİ DEĞİŞTİR", callback_data=_secured_callback("i", post)
                ),
            ],
            [InlineKeyboardButton("❌ REDDET", callback_data=_secured_callback("x", post))],
        ]
    )


def publish_keyboard(post: Post) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 HEMEN PAYLAŞ", callback_data=f"p:{post.id}"),
                InlineKeyboardButton("⏰ PLANLA", callback_data=f"s:{post.id}"),
            ]
        ]
    )


async def send_draft(bot: Bot, post: Post, chat_id: int | None = None) -> None:
    destination = chat_id or settings.telegram_admin_chat_id
    if destination is None:
        raise ValueError("TELEGRAM_ADMIN_CHAT_ID is required")
    if post.image_path:
        image, _ = await asyncio.to_thread(_content_addressed_image, post.image_path)
        await bot.send_document(
            destination,
            document=InputFile(image, filename=Path(post.image_path).name),
            caption="Önerilen görsel — LinkedIn'e gönderilecek özgün PNG dosyası",
        )
    source_lines = "\n".join(
        f"• {source.get('title', 'Kaynak')}: {source.get('resolved_url') or source.get('url')}"
        for source in post.sources[:3]
    )
    metadata = (
        "📌 LinkedIn Gönderisi Hazır\n\n"
        f"Konu: {post.topic}\nTür: {post.content_type}\n\n"
        f"Kaynaklar:\n{source_lines or 'Görüş yazısı; harici iddia yok.'}\n\n"
        f"Versiyon: {len(post.revisions)} | Kalite: "
        f"{post.scores.get('quality', {}).get('score', '-')}"
    )
    await bot.send_message(destination, metadata[:4096])
    await bot.send_message(destination, canonical_commentary(post))
    await bot.send_message(
        destination,
        "Yukarıdaki metin ve görselin tamamını inceledikten sonra karar verin.",
        reply_markup=review_keyboard(post),
    )


async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    topic = " ".join(context.args).strip()
    if not topic:
        await update.message.reply_text("Kullanım: /generate konu")
        return
    await update.message.reply_text("Kaynaklar araştırılıyor ve taslak hazırlanıyor…")
    async with SessionLocal() as db:
        post = await create_draft(db, topic, generation_slot=f"telegram:{update.update_id}")
    from .notifier import deliver_notification

    await deliver_notification(post.id, context.bot)


def _parse_secured_payload(payload: str) -> tuple[uuid.UUID, str]:
    raw_id, nonce = payload.split(":", 1)
    return uuid.UUID(raw_id), nonce


def _actor(update: Update) -> str:
    return (
        f"telegram:user:{update.effective_user.id}:"
        f"chat:{update.effective_chat.id}:update:{update.update_id}"
    )


async def _set_pending_action(
    update: Update,
    kind: str,
    *,
    post_id: uuid.UUID | None = None,
    nonce: str | None = None,
    payload: dict | None = None,
) -> None:
    async with SessionLocal() as db:
        await db.execute(
            insert(TelegramPendingAction)
            .values(
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                kind=kind,
                post_id=post_id,
                nonce=nonce,
                payload=payload or {},
            )
            .on_conflict_do_update(
                index_elements=[TelegramPendingAction.user_id],
                set_={
                    "chat_id": update.effective_chat.id,
                    "kind": kind,
                    "post_id": post_id,
                    "nonce": nonce,
                    "payload": payload or {},
                    "updated_at": datetime.now(UTC),
                },
            )
        )
        await db.commit()


async def _get_pending_action(update: Update) -> dict | None:
    async with SessionLocal() as db:
        pending = await db.get(TelegramPendingAction, update.effective_user.id)
        if not pending or pending.chat_id != update.effective_chat.id:
            return None
        updated_at = pending.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if updated_at < datetime.now(UTC) - timedelta(hours=24):
            await db.delete(pending)
            await db.commit()
            return None
        return {
            "kind": pending.kind,
            "post_id": pending.post_id,
            "nonce": pending.nonce,
            "payload": dict(pending.payload),
        }


async def _clear_pending_action(update: Update, expected_kind: str | None = None) -> None:
    criteria = [TelegramPendingAction.user_id == update.effective_user.id]
    if expected_kind:
        criteria.append(TelegramPendingAction.kind == expected_kind)
    async with SessionLocal() as db:
        await db.execute(delete(TelegramPendingAction).where(*criteria))
        await db.commit()


async def _remove_inline_keyboard(query) -> None:
    try:
        await query.edit_message_reply_markup(None)
    except Exception:  # noqa: BLE001 - UI cleanup must not undo a committed domain action
        log.warning("telegram_keyboard_cleanup_failed")


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    query = update.callback_query
    try:
        await query.answer()
    except Exception:  # noqa: BLE001 - an old callback can still be processed idempotently
        log.warning("telegram_callback_ack_failed")
    action = "unknown"
    payload = ""
    try:
        action, payload = query.data.split(":", 1)
        async with SessionLocal() as db:
            if action == "a":
                post_id, nonce = _parse_secured_payload(payload)
                post = await approve(db, post_id, nonce, actor=_actor(update))
                await _clear_pending_action(update)
                await query.message.reply_text(
                    "✅ Onay kaydedildi. Ne zaman paylaşılsın?",
                    reply_markup=publish_keyboard(post),
                )
                await _remove_inline_keyboard(query)
            elif action in {"r", "g", "i", "x"}:
                post_id, nonce = _parse_secured_payload(payload)
                post = (await db.execute(select(Post).where(Post.id == post_id))).scalar_one()
                if post.status != PostStatus.PENDING_APPROVAL or post.approval_nonce != nonce:
                    raise ValueError("Bu buton eski bir taslağa ait")
                if action == "r":
                    await db.rollback()
                    await _set_pending_action(update, "revision", post_id=post_id, nonce=nonce)
                    await query.message.reply_text("Metin revizyon isteğinizi doğal dille yazın.")
                elif action == "i":
                    await db.rollback()
                    await _set_pending_action(update, "image", post_id=post_id, nonce=nonce)
                    await query.message.reply_text("Yeni görseli nasıl istediğinizi yazın.")
                elif action == "g":
                    await db.rollback()
                    await _clear_pending_action(update)
                    post = await revise(
                        db,
                        post_id,
                        "Aynı kaynaklarla tamamen yeni bir anlatım üret.",
                        actor=_actor(update),
                        expected_nonce=nonce,
                    )
                    from .notifier import deliver_notification

                    await deliver_notification(post.id, context.bot)
                else:
                    await db.rollback()
                    await _clear_pending_action(update)
                    await reject(db, post_id, nonce, actor=_actor(update))
                    await query.message.reply_text("Reddedildi.")
                    await _remove_inline_keyboard(query)
            elif action == "p":
                await _clear_pending_action(update)
                post = await publish_approved(db, uuid.UUID(payload), actor=_actor(update))
                from .notifier import deliver_notification

                await deliver_notification(post.id, context.bot)
                await _remove_inline_keyboard(query)
            elif action == "s":
                post_id = uuid.UUID(payload)
                post = await db.get(Post, post_id)
                if not post or post.status != PostStatus.APPROVED:
                    raise ValueError("Yalnızca onaylı bir gönderi planlanabilir")
                await db.rollback()
                await _set_pending_action(update, "schedule", post_id=post_id)
                await query.message.reply_text(
                    "Yayın zamanını yazın. Örnek: yarın 09:15 veya 12 Eylül 18:30"
                )
    except (ValueError, LookupError) as exc:
        await query.message.reply_text(f"İşlem yapılamadı: {exc}")
    except Exception:
        log.exception("telegram_callback_failed", action=action)
        if action == "p":
            from .notifier import deliver_notification

            try:
                post_id = uuid.UUID(payload)
            except (ValueError, AttributeError):
                pass
            else:
                await deliver_notification(post_id, context.bot)
        raise


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    text = update.message.text.strip()
    try:
        request = await _get_pending_action(update)
        if request and text.casefold() in {"iptal", "vazgeç", "cancel"}:
            await _clear_pending_action(update)
            await update.message.reply_text("Bekleyen işlem iptal edildi.")
        elif request and request["kind"] == "revision":
            async with SessionLocal() as db:
                post = await revise(
                    db,
                    request["post_id"],
                    text,
                    actor=_actor(update),
                    expected_nonce=request["nonce"],
                )
            from .notifier import deliver_notification

            await deliver_notification(post.id, context.bot)
            await _clear_pending_action(update, "revision")
        elif request and request["kind"] == "image":
            async with SessionLocal() as db:
                post = await revise_image(
                    db,
                    request["post_id"],
                    text,
                    actor=_actor(update),
                    expected_nonce=request["nonce"],
                )
            from .notifier import deliver_notification

            await deliver_notification(post.id, context.bot)
            await _clear_pending_action(update, "image")
        elif request and request["kind"] == "schedule":
            parsed = dateparser.parse(
                text,
                languages=["tr", "en"],
                settings={
                    "TIMEZONE": settings.timezone,
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future",
                },
            )
            if parsed is None:
                raise ValueError("Tarih anlaşılamadı")
            async with SessionLocal() as db:
                post = await schedule_approved(db, request["post_id"], parsed, actor=_actor(update))
            await _clear_pending_action(update, "schedule")
            await update.message.reply_text(
                "⏰ Planlandı: "
                f"{post.scheduled_at.astimezone(ZoneInfo(settings.timezone)).strftime('%d.%m.%Y %H:%M %Z')}"
            )
        elif request and request["kind"] == "confirm_admin":
            if text.casefold() not in {"evet", "onayla", "yes", "confirm"}:
                await update.message.reply_text(
                    "Bu değişiklik için ‘evet’ yazın veya iptal etmek için ‘iptal’ yazın."
                )
                return
            await handle_admin_intent(
                update,
                context,
                "",
                parsed_intent=request["payload"]["intent"],
                confirmed=True,
                original_message_digest=request["payload"]["message_digest"],
            )
            await _clear_pending_action(update, "confirm_admin")
        elif request:
            await _clear_pending_action(update)
            raise ValueError("Bekleyen işlem türü geçersizdi ve temizlendi")
        else:
            await handle_admin_intent(update, context, text)
    except (ValueError, LookupError) as exc:
        await update.message.reply_text(f"İşlem yapılamadı: {exc}")


async def _mutate_setting(key: str, default: dict, mutator) -> dict:
    async with SessionLocal() as db:
        await db.execute(
            insert(Setting)
            .values(key=key, value=default)
            .on_conflict_do_nothing(index_elements=[Setting.key])
        )
        row = await db.get(Setting, key, with_for_update=True)
        current = dict(row.value) if row and isinstance(row.value, dict) else dict(default)
        updated = mutator(current)
        if not isinstance(updated, dict):
            raise TypeError("Setting mutation must return a JSON object")
        row.value = updated
        await db.commit()
        return updated


async def _current_calendar() -> dict:
    async with SessionLocal() as db:
        row = await db.get(Setting, "content_calendar")
    raw = dict(row.value) if row and isinstance(row.value, dict) else {}
    calendar = {**CALENDAR_DEFAULTS, **raw}
    weekdays = calendar.get("weekdays")
    if (
        not isinstance(weekdays, list)
        or not weekdays
        or not all(
            isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6 for day in weekdays
        )
    ):
        calendar["weekdays"] = CALENDAR_DEFAULTS["weekdays"]
    hour = calendar.get("hour")
    minute = calendar.get("minute")
    calendar["hour"] = (
        hour if isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23 else 9
    )
    calendar["minute"] = (
        minute
        if isinstance(minute, int) and not isinstance(minute, bool) and 0 <= minute <= 59
        else 0
    )
    return calendar


def _clean_topics(raw_topics: list[str]) -> list[str]:
    topics = []
    seen = set()
    for raw_topic in raw_topics:
        topic = " ".join(str(raw_topic).split())
        folded = topic.casefold()
        if topic and len(topic) <= 160 and folded not in seen:
            topics.append(topic)
            seen.add(folded)
    return topics


def _normalize_weekdays(raw_days: list) -> list[int]:
    if not isinstance(raw_days, list):
        return []
    return sorted(
        {
            day
            for day in raw_days
            if isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6
        }
    )


def _weekdays_for_frequency(frequency: int, requested_days: list) -> list[int]:
    frequency_days = {
        1: [2],
        2: [1, 4],
        3: [0, 2, 4],
        4: [0, 1, 3, 4],
        5: [0, 1, 2, 3, 4],
        6: [0, 1, 2, 3, 4, 5],
        7: [0, 1, 2, 3, 4, 5, 6],
    }
    if (
        not isinstance(frequency, int)
        or isinstance(frequency, bool)
        or frequency not in frequency_days
    ):
        raise ValueError("Haftalık sıklık 1 ile 7 arasında olmalı")
    normalized = _normalize_weekdays(requested_days)
    return normalized if len(normalized) == frequency else frequency_days[frequency]


def _parse_skip_deadline(phrase: str, now: datetime | None = None) -> datetime | None:
    current = now or datetime.now(ZoneInfo(settings.timezone))
    normalized = " ".join(phrase.casefold().split())
    if normalized in {"gelecek hafta", "gelecek haftaya kadar", "önümüzdeki hafta"}:
        return (current + timedelta(days=7 - current.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if normalized in {"gelecek ay", "gelecek aya kadar", "önümüzdeki ay"}:
        return (current.replace(day=28) + timedelta(days=4)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return dateparser.parse(
        phrase,
        languages=["tr", "en"],
        settings={
            "TIMEZONE": settings.timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": current,
        },
    )


async def _record_admin_action(
    update: Update, action: str, message_digest: str, confirmed: bool
) -> None:
    async with SessionLocal() as db:
        await audit(
            db,
            "telegram_admin_action",
            None,
            actor=_actor(update),
            details={
                "action": action,
                "message_digest": message_digest,
                "confirmed": confirmed,
            },
        )
        await db.commit()


async def handle_admin_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: str,
    *,
    parsed_intent: dict | None = None,
    confirmed: bool = False,
    original_message_digest: str | None = None,
) -> None:
    intent = parsed_intent or await ContentEngine().parse_admin_intent(message)
    action = intent["intent"]
    confidence = intent.get("confidence", 0)
    if action == "unknown" or not isinstance(confidence, int) or confidence < 85:
        await update.message.reply_text(
            "İsteği güvenle eşleştiremedim. Komutları görmek için /help yazın."
        )
        return
    message_digest = original_message_digest or sha256(message.encode()).hexdigest()
    if action in CONFIRMATION_REQUIRED and not confirmed:
        await _set_pending_action(
            update,
            "confirm_admin",
            payload={"intent": intent, "message_digest": message_digest},
        )
        await update.message.reply_text(
            f"{ACTION_LABELS[action].capitalize()} işlemini uygulamam için ‘evet’ yazın. "
            "Vazgeçmek için ‘iptal’ yazabilirsiniz."
        )
        return
    if action == "add_topics":
        requested_topics = _clean_topics(intent["topics"])
        if not requested_topics:
            raise ValueError("Eklenecek geçerli bir konu bulunamadı")
        async with SessionLocal() as db:
            for topic in requested_topics:
                await db.execute(
                    insert(Topic)
                    .values(name=topic.strip(), active=True)
                    .on_conflict_do_update(
                        index_elements=[func.lower(Topic.name)], set_={"active": True}
                    )
                )
            await db.commit()
        await update.message.reply_text("Eklendi: " + ", ".join(requested_topics))
    elif action == "remove_topics":
        requested_topics = _clean_topics(intent["topics"])
        if not requested_topics:
            raise ValueError("Kaldırılacak geçerli bir konu bulunamadı")
        async with SessionLocal() as db:
            for topic in requested_topics:
                row = (
                    await db.execute(
                        select(Topic).where(func.lower(Topic.name) == topic.strip().lower())
                    )
                ).scalar_one_or_none()
                if row:
                    row.active = False
            await db.commit()
        await update.message.reply_text("Konu listesi güncellendi.")
    elif action in {"pause", "resume"}:
        enabled = action == "pause"
        await _mutate_setting(
            "system_paused",
            {"enabled": False},
            lambda current: {**current, "enabled": enabled},
        )
        if not enabled:

            def clear_skip(current):
                current.pop("skip_until", None)
                return current

            await _mutate_setting("content_calendar", CALENDAR_DEFAULTS, clear_skip)
        await update.message.reply_text(
            "Otomatik üretim durduruldu." if enabled else "Otomatik üretim açıldı."
        )
    elif action == "generate":
        topic = ", ".join(_clean_topics(intent["topics"])) or message
        await update.message.reply_text("Taslak hazırlanıyor…")
        async with SessionLocal() as db:
            post = await create_draft(
                db,
                topic,
                generation_slot=f"telegram:{update.update_id}",
            )
        from .notifier import deliver_notification

        await deliver_notification(post.id, context.bot)
    elif action in {"set_frequency", "set_days", "set_time"}:
        new_weekdays = None
        if action == "set_days":
            new_weekdays = _normalize_weekdays(intent["weekdays"])
            if not new_weekdays:
                raise ValueError("Takvim için en az bir gün belirtin")
        elif action == "set_frequency":
            frequency = intent["frequency"]
            new_weekdays = _weekdays_for_frequency(frequency, intent["weekdays"])
        if action == "set_time" and intent["hour"] is None:
            raise ValueError("Takvim saati anlaşılamadı")

        def change_calendar(current):
            if new_weekdays is not None:
                current["weekdays"] = new_weekdays
            if intent["hour"] is not None:
                current["hour"] = intent["hour"]
            if intent["minute"] is not None:
                current["minute"] = intent["minute"]
            return current

        await _mutate_setting("content_calendar", CALENDAR_DEFAULTS, change_calendar)
        await update.message.reply_text("İçerik takvimi güncellendi.")
    elif action == "skip_until":
        now = datetime.now(ZoneInfo(settings.timezone))
        parsed = _parse_skip_deadline(intent["until"] or message, now)
        if not parsed:
            raise ValueError("Duraklatma bitiş tarihi anlaşılamadı")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(settings.timezone))
        if parsed <= now:
            raise ValueError("Duraklatma bitiş tarihi gelecekte olmalı")
        await _mutate_setting(
            "content_calendar",
            CALENDAR_DEFAULTS,
            lambda current: {**current, "skip_until": parsed.isoformat()},
        )
        await update.message.reply_text(
            f"Otomatik üretim {parsed:%d.%m.%Y %H:%M} tarihine kadar atlandı."
        )
    elif action == "style_preference":
        preference = " ".join(str(intent["preference"] or message).split())[:240]
        if not preference:
            raise ValueError("Kaydedilecek yazım tercihi boş olamaz")

        def append_preference(current):
            stored = current.get("preferences", [])
            preferences = list(stored) if isinstance(stored, list) else []
            if preference not in preferences:
                preferences.append(preference)
            current["preferences"] = preferences[-20:]
            return current

        await _mutate_setting(
            "style_profile",
            {"preferences": [], "revision_signals": {}},
            append_preference,
        )
        await update.message.reply_text("Yazım tercihi kaydedildi.")
    else:
        await update.message.reply_text("Bu yönetim işlemi desteklenmiyor.")
        return
    await _record_admin_action(update, action, message_digest, confirmed)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    async with SessionLocal() as db:
        rows = (await db.execute(select(Post.status))).scalars().all()
        paused = await _paused(db)
    counts = {post_status.value: rows.count(post_status) for post_status in set(rows)}
    await update.message.reply_text(
        f"Sistem: {'duraklatıldı' if paused else 'aktif'} | Dry-run: {settings.dry_run}\n"
        + "\n".join(f"{key}: {value}" for key, value in sorted(counts.items()))
    )


async def topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    async with SessionLocal() as db:
        names = (
            (
                await db.execute(
                    select(Topic.name).where(Topic.active.is_(True)).order_by(Topic.name)
                )
            )
            .scalars()
            .all()
        )
    await update.message.reply_text("Konular:\n" + "\n".join(f"• {name}" for name in names))


async def add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Kullanım: /addtopic konu")
        return
    if len(name) > 160:
        await update.message.reply_text("Konu en fazla 160 karakter olabilir.")
        return
    async with SessionLocal() as db:
        await db.execute(
            insert(Topic)
            .values(name=name, active=True)
            .on_conflict_do_update(index_elements=[func.lower(Topic.name)], set_={"active": True})
        )
        await audit(
            db,
            "telegram_topic_added",
            None,
            actor=_actor(update),
            details={"topic": name},
        )
        await db.commit()
    await update.message.reply_text(f"Eklendi: {name}")


async def remove_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Kullanım: /removetopic konu")
        return
    async with SessionLocal() as db:
        topic = (
            await db.execute(select(Topic).where(func.lower(Topic.name) == name.lower()))
        ).scalar_one_or_none()
        if topic:
            topic.active = False
            await audit(
                db,
                "telegram_topic_removed",
                None,
                actor=_actor(update),
                details={"topic": name},
            )
            await db.commit()
    await update.message.reply_text(f"{'Kaldırıldı' if topic else 'Bulunamadı'}: {name}")


async def _paused(db) -> bool:
    setting = await db.get(Setting, "system_paused")
    return bool(setting and setting.value.get("enabled"))


async def set_pause(update: Update, enabled: bool) -> None:
    if not is_admin(update):
        return
    await _mutate_setting(
        "system_paused",
        {"enabled": False},
        lambda current: {**current, "enabled": enabled},
    )
    if not enabled:

        def clear_skip(current):
            current.pop("skip_until", None)
            return current

        await _mutate_setting("content_calendar", CALENDAR_DEFAULTS, clear_skip)
    action = "pause" if enabled else "resume"
    await _record_admin_action(update, action, sha256(f"/{action}".encode()).hexdigest(), True)
    message = "Otomatik üretim durduruldu." if enabled else "Otomatik üretim açıldı."
    await update.message.reply_text(message)


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_pause(update, True)


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_pause(update, False)


async def list_posts(update: Update, statuses: list[PostStatus], title: str) -> None:
    if not is_admin(update):
        return
    async with SessionLocal() as db:
        posts = (
            (
                await db.execute(
                    select(Post)
                    .where(Post.status.in_(statuses))
                    .order_by(Post.created_at.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
    lines = [f"• {post.topic} — {post.status.value} — {str(post.id)[:8]}" for post in posts]
    await update.message.reply_text(f"{title}\n" + ("\n".join(lines) or "Kayıt yok."))


async def drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await list_posts(
        update,
        [PostStatus.DRAFT, PostStatus.PENDING_APPROVAL, PostStatus.REVISION_REQUESTED],
        "Taslaklar",
    )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await list_posts(update, [PostStatus.PUBLISHED, PostStatus.REJECTED], "Geçmiş")


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    calendar = await _current_calendar()
    async with SessionLocal() as db:
        posts = (
            (
                await db.execute(
                    select(Post)
                    .where(Post.status == PostStatus.SCHEDULED)
                    .order_by(Post.scheduled_at)
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
    days = ", ".join(WEEKDAY_LABELS[day] for day in calendar["weekdays"])
    skip_until = calendar.get("skip_until")
    skip_line = f"\nAtlama bitişi: {skip_until}" if skip_until else ""
    lines = [
        "• "
        f"{post.scheduled_at.astimezone(ZoneInfo(settings.timezone)):%d.%m.%Y %H:%M} — "
        f"{post.topic} — {str(post.id)[:8]}"
        for post in posts
        if post.scheduled_at
    ]
    await update.message.reply_text(
        f"Otomatik üretim: {days} {calendar['hour']:02d}:{calendar['minute']:02d}"
        f"{skip_line}\n\nPlanlanan LinkedIn gönderileri:\n" + ("\n".join(lines) or "Kayıt yok.")
    )


async def analytics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    async with SessionLocal() as db:
        published = await db.scalar(
            select(func.count()).select_from(Post).where(Post.status == PostStatus.PUBLISHED)
        )
        latest = (
            select(
                AnalyticsSnapshot.post_id,
                func.max(AnalyticsSnapshot.collected_at).label("collected_at"),
            )
            .group_by(AnalyticsSnapshot.post_id)
            .subquery()
        )
        values = (
            await db.execute(
                select(
                    func.coalesce(func.sum(AnalyticsSnapshot.impressions), 0),
                    func.coalesce(func.sum(AnalyticsSnapshot.reactions), 0),
                    func.coalesce(func.sum(AnalyticsSnapshot.comments), 0),
                    func.coalesce(func.sum(AnalyticsSnapshot.reposts), 0),
                    func.coalesce(func.sum(AnalyticsSnapshot.clicks), 0),
                ).join(
                    latest,
                    (AnalyticsSnapshot.post_id == latest.c.post_id)
                    & (AnalyticsSnapshot.collected_at == latest.c.collected_at),
                )
            )
        ).one()
    engagement = calculate_engagement_rate(*map(int, values))
    await update.message.reply_text(
        f"📊 LinkedIn Özeti\nGönderi: {published}\nGösterim: {values[0]}\n"
        f"Tepki: {values[1]}\nYorum: {values[2]}\nRepost: {values[3]}\n"
        f"Bağlantı tıklaması: {values[4]}\nEtkileşim: %{engagement:.2f}\n\n"
        "Not: Otomatik metrik toplama LinkedIn analitik izni verilince etkinleşir."
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(
        f"Timezone: {settings.timezone}\nDry-run: {settings.dry_run}\n"
        f"Metin modeli: {settings.openai_text_model}\nGörsel modeli: {settings.openai_image_model}"
    )


async def reconcile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Kullanım: /reconcile UUID published URN veya /reconcile UUID not-published"
        )
        return
    try:
        post_id = uuid.UUID(context.args[0])
        resolution = context.args[1].casefold()
        urn = context.args[2] if len(context.args) == 3 else None
        if len(context.args) > 3:
            raise ValueError("Çok fazla parametre verildi")
        async with SessionLocal() as db:
            post = await reconcile_publication(
                db,
                post_id,
                resolution,
                linkedin_urn=urn,
                actor=_actor(update),
            )
    except (ValueError, LookupError) as exc:
        await update.message.reply_text(f"Uzlaştırma yapılamadı: {exc}")
        return
    if resolution == "not-published":
        await update.message.reply_text(
            "Yayın bulunamadı olarak işaretlendi. Yeniden göndermek için "
            "🚀 HEMEN PAYLAŞ düğmesini ayrıca seçin.",
            reply_markup=publish_keyboard(post),
        )
    else:
        await update.message.reply_text(f"✅ Yayın kaydı uzlaştırıldı.\n{post.linkedin_url}")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await _clear_pending_action(update)
    await update.message.reply_text("Bekleyen işlem temizlendi.")


async def retry_alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    async with SessionLocal() as db:
        result = await db.execute(
            sql_update(NotificationOutbox)
            .where(NotificationOutbox.status == "dead")
            .values(
                status="pending",
                attempts=0,
                next_attempt_at=datetime.now(UTC),
                claimed_at=None,
            )
        )
        await audit(
            db,
            "telegram_notification_retries_reset",
            None,
            actor=_actor(update),
            details={"count": result.rowcount},
        )
        await db.commit()
    await update.message.reply_text(f"Yeniden sıraya alınan bildirim: {result.rowcount}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(
        "/generate konu — taslak üret\n/topics — konuları listele\n"
        "/addtopic konu — konu ekle\n/removetopic konu — konu kaldır\n"
        "/drafts — bekleyen taslaklar\n/history — geçmiş\n/schedule — planlananlar\n"
        "/analytics — performans özeti\n/pause — otomatik üretimi durdur\n"
        "/resume — devam et\n/cancel — bekleyen işlemi iptal et\n"
        "/retryalerts — tükenen bildirimleri yeniden dene\n"
        "/reconcile UUID published URN — bulunan LinkedIn yayınını kaydet\n"
        "/reconcile UUID not-published — bulunmayan yayını onaylı duruma getir\n"
        "/status — sistem durumu\n/settings — ayarlar\n\n"
        "Ayrıca doğal dille konu, sıklık, gün/saat ve yazım tercihi belirtebilirsiniz."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error_type = type(context.error).__name__
    log.error("telegram_handler_failed", error_type=error_type)
    update_id = getattr(update, "update_id", None)
    should_notify = True
    if isinstance(update_id, int):
        context.application.bot_data.setdefault("_failed_updates", {})[update_id] = error_type
        notified = context.application.bot_data.setdefault("_failed_update_notified", set())
        should_notify = update_id not in notified
        notified.add(update_id)
    if settings.telegram_admin_chat_id and should_notify:
        try:
            await context.bot.send_message(
                settings.telegram_admin_chat_id,
                "⚠️ Telegram işlemi başarısız oldu. İçerik veritabanında korunuyor.",
            )
        except Exception:  # noqa: BLE001 - last-resort notification must not crash the app
            log.error("telegram_failure_notification_failed")


def build_application() -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .updater(None)
        .build()
    )
    for command, handler in {
        "start": help_command,
        "help": help_command,
        "generate": generate,
        "status": status,
        "topics": topics,
        "addtopic": add_topic,
        "removetopic": remove_topic,
        "pause": pause,
        "resume": resume,
        "drafts": drafts,
        "history": history,
        "schedule": schedule,
        "analytics": analytics,
        "settings": settings_command,
        "reconcile": reconcile_command,
        "cancel": cancel_command,
        "retryalerts": retry_alerts_command,
    }.items():
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    return app
