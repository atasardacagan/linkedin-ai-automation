from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .ai import ContentEngine
from .analytics import collect_analytics, send_weekly_report, warn_token_expiry
from .config import settings
from .db import SessionLocal
from .logging import safe_exception_message
from .models import GenerationRun, NotificationOutbox, Post, PostStatus, Setting, Topic
from .notifier import drain_notifications
from .research import validate_sources
from .service import audit, create_draft, publish_approved

log = structlog.get_logger()
scheduler = AsyncIOScheduler(timezone=settings.timezone)


async def generate_scheduled_draft():
    if not settings.auto_generation_enabled:
        return
    now = datetime.now(ZoneInfo(settings.timezone))
    now_utc = now.astimezone(UTC)
    async with SessionLocal() as db:
        pause_setting = await db.get(Setting, "system_paused")
        if pause_setting and pause_setting.value.get("enabled"):
            return
        calendar_setting = await db.get(Setting, "content_calendar", with_for_update=True)
        calendar = (
            dict(calendar_setting.value)
            if calendar_setting and isinstance(calendar_setting.value, dict)
            else {"weekdays": [0, 2, 4], "hour": 9, "minute": 0}
        )
        weekdays = calendar.get("weekdays", [0, 2, 4])
        if (
            not isinstance(weekdays, list)
            or not weekdays
            or not all(
                isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6
                for day in weekdays
            )
        ):
            weekdays = [0, 2, 4]
        hour = calendar.get("hour", 9)
        minute = calendar.get("minute", 0)
        hour = (
            hour if isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23 else 9
        )
        minute = (
            minute
            if isinstance(minute, int) and not isinstance(minute, bool) and 0 <= minute <= 59
            else 0
        )
        skip_until = calendar.get("skip_until")
        if isinstance(skip_until, str):
            try:
                skip_deadline = datetime.fromisoformat(skip_until)
                if skip_deadline.tzinfo is None:
                    skip_deadline = skip_deadline.replace(tzinfo=ZoneInfo(settings.timezone))
            except ValueError:
                log.warning("invalid_content_calendar_skip_until")
                calendar.pop("skip_until", None)
            else:
                if skip_deadline > now:
                    await db.rollback()
                    return
                calendar.pop("skip_until", None)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        seconds_after_target = (now - target).total_seconds()
        if calendar_setting:
            calendar_setting.value = calendar
        else:
            db.add(Setting(key="content_calendar", value=calendar))
        if now.weekday() in weekdays and 0 <= seconds_after_target < 15 * 60:
            slot = f"{settings.timezone}:{now.date().isoformat()}T{hour:02d}:{minute:02d}"
            await db.execute(
                insert(GenerationRun)
                .values(slot=slot, status="pending", next_attempt_at=now_utc)
                .on_conflict_do_nothing(index_elements=[GenerationRun.slot])
            )
        await db.execute(
            update(GenerationRun)
            .where(
                GenerationRun.status == "processing",
                GenerationRun.claimed_at < now_utc - timedelta(minutes=30),
            )
            .values(status="retryable", claimed_at=None, next_attempt_at=now_utc)
        )
        generation_run = (
            await db.execute(
                select(GenerationRun)
                .where(
                    GenerationRun.status.in_(["pending", "retryable"]),
                    GenerationRun.next_attempt_at <= now_utc,
                    GenerationRun.attempts < 5,
                )
                .order_by(GenerationRun.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if not generation_run:
            await db.commit()
            return
        generation_run.status = "processing"
        generation_run.claimed_at = now_utc
        generation_run.attempts += 1
        run_slot = generation_run.slot
        run_attempt = generation_run.attempts
        existing_post = await db.scalar(select(Post).where(Post.generation_slot == run_slot))
        topic_names = (
            (await db.execute(select(Topic.name).where(Topic.active.is_(True)))).scalars().all()
        )
        await db.commit()
    if existing_post:
        await _complete_generation_run(run_slot, post_id=existing_post.id)
        return
    if not topic_names:
        await _complete_generation_run(run_slot, note="no active topics", status="skipped")
        return
    try:
        candidates = await ContentEngine().discover(topic_names)
        viable = [candidate for candidate in candidates if candidate["total"] >= 65]
        selected = None
        sources = []
        for candidate in viable[:5]:
            sources = await validate_sources([candidate["source"]])
            if sources:
                selected = candidate
                break
        if not selected:
            await _complete_generation_run(
                run_slot,
                note="no validated content opportunity above threshold",
                status="completed_noop",
            )
            return
        async with SessionLocal() as db:
            post = await create_draft(
                db,
                selected["topic"],
                sources=sources,
                generation_slot=run_slot,
            )
        await _complete_generation_run(run_slot, post_id=post.id)
        await drain_notifications(limit=1)
    except Exception as exc:
        delay = min(3600, 60 * (2 ** max(run_attempt - 1, 0)))
        retryable = run_attempt < 5
        async with SessionLocal() as db:
            current = await db.get(GenerationRun, run_slot, with_for_update=True)
            if current:
                current.status = "retryable" if retryable else "failed"
                current.claimed_at = None
                current.next_attempt_at = (
                    datetime.now(UTC) + timedelta(seconds=delay) if retryable else None
                )
                current.completed_at = None if retryable else datetime.now(UTC)
                current.last_error = safe_exception_message(exc)
                await db.commit()
        log.exception(
            "content_generation_failed",
            generation_slot=run_slot,
            retry_scheduled=retryable,
        )


async def _complete_generation_run(
    slot: str,
    *,
    post_id=None,
    note: str | None = None,
    status: str = "completed",
) -> None:
    async with SessionLocal() as db:
        generation_run = await db.get(GenerationRun, slot, with_for_update=True)
        if not generation_run:
            return
        generation_run.status = status
        generation_run.claimed_at = None
        generation_run.next_attempt_at = None
        generation_run.completed_at = datetime.now(UTC)
        generation_run.post_id = post_id
        generation_run.last_error = note
        await db.commit()


async def reconcile_stale_publications() -> None:
    cutoff = datetime.now(UTC) - timedelta(minutes=15)
    async with SessionLocal() as db:
        posts = (
            (
                await db.execute(
                    select(Post)
                    .where(
                        Post.status == PostStatus.PUBLISHING,
                        Post.requires_reconciliation.is_(False),
                        Post.updated_at < cutoff,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        for post in posts:
            post.requires_reconciliation = True
            post.last_error = "Publishing lease expired; LinkedIn outcome must be reconciled"
            db.add(
                NotificationOutbox(
                    idempotency_key=f"stale-publishing:{post.id}",
                    post_id=post.id,
                    kind="publish_reconciliation_required",
                )
            )
            await audit(db, "linkedin_publish_lease_expired", post)
        await db.commit()


async def publish_due_posts():
    async with SessionLocal() as db:
        ids = (
            (
                await db.execute(
                    select(Post.id)
                    .where(
                        Post.requires_reconciliation.is_(False),
                        Post.publish_attempts < 5,
                        or_(
                            (Post.status == PostStatus.SCHEDULED)
                            & (Post.scheduled_at <= datetime.now(UTC)),
                            (Post.status == PostStatus.PUBLISH_FAILED)
                            & (Post.next_publish_attempt_at <= datetime.now(UTC)),
                        ),
                    )
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
    for post_id in ids:
        async with SessionLocal() as db:
            try:
                await publish_approved(
                    db,
                    post_id,
                    scheduled_worker=True,
                    actor="scheduler",
                )
            except Exception:
                log.exception("scheduled_publish_failed", post_id=str(post_id))


def start_scheduler():
    scheduler.add_job(
        generate_scheduled_draft,
        "interval",
        minutes=5,
        id="generate",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        warn_token_expiry,
        "cron",
        hour=8,
        minute=0,
        id="token_expiry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        collect_analytics,
        "cron",
        hour=3,
        minute=30,
        id="analytics",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_weekly_report,
        "cron",
        day_of_week="mon",
        hour=8,
        minute=30,
        id="weekly_report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        drain_notifications,
        "interval",
        seconds=20,
        id="notifications",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        publish_due_posts,
        "interval",
        minutes=1,
        id="publish",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reconcile_stale_publications,
        "interval",
        minutes=5,
        id="publish_reconciliation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
