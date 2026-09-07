from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from telegram import Bot

from .config import settings
from .db import SessionLocal
from .linkedin import LinkedInClient
from .models import AnalyticsSnapshot, Post, PostStatus, Setting

log = structlog.get_logger()


def calculate_engagement_rate(
    impressions: int, reactions: int, comments: int, reposts: int, clicks: int
) -> float:
    if impressions <= 0:
        return 0.0
    return 100 * (reactions + comments + reposts + clicks) / impressions


async def _reserve_daily_call_budget(call_count: int) -> bool:
    today = datetime.now(UTC).date().isoformat()
    key = f"linkedin_analytics_usage:{today}"
    async with SessionLocal() as db:
        await db.execute(
            insert(Setting)
            .values(key=key, value={"date": today, "calls": 0})
            .on_conflict_do_nothing(index_elements=[Setting.key])
        )
        usage = await db.get(Setting, key, with_for_update=True)
        stored = usage.value.get("calls", 0) if isinstance(usage.value, dict) else 0
        used = stored if isinstance(stored, int) and stored >= 0 else 0
        if used + call_count > settings.linkedin_analytics_daily_call_budget:
            await db.rollback()
            return False
        usage.value = {"date": today, "calls": used + call_count}
        await db.commit()
        return True


async def collect_analytics() -> None:
    if not settings.linkedin_analytics_enabled:
        return
    now = datetime.now(UTC)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(days=settings.linkedin_analytics_lookback_days)
    metric_names = ("IMPRESSION", "REACTION", "COMMENT", "RESHARE", "LINK_CLICKS")
    post_budget = max(1, settings.linkedin_analytics_daily_call_budget // len(metric_names))
    async with SessionLocal() as db:
        latest = (
            select(
                AnalyticsSnapshot.post_id,
                func.max(AnalyticsSnapshot.collected_at).label("collected_at"),
            )
            .group_by(AnalyticsSnapshot.post_id)
            .subquery()
        )
        posts = (
            (
                await db.execute(
                    select(Post)
                    .outerjoin(latest, latest.c.post_id == Post.id)
                    .where(
                        Post.status == PostStatus.PUBLISHED,
                        Post.linkedin_post_urn.is_not(None),
                        Post.published_at >= cutoff,
                        or_(latest.c.collected_at.is_(None), latest.c.collected_at < start_of_day),
                    )
                    .order_by(latest.c.collected_at.asc().nullsfirst(), Post.published_at.desc())
                    .limit(post_budget)
                )
            )
            .scalars()
            .all()
        )
    client = LinkedInClient()
    for post in posts:
        if not await _reserve_daily_call_budget(len(metric_names)):
            log.info(
                "linkedin_analytics_daily_budget_exhausted",
                daily_budget=settings.linkedin_analytics_daily_call_budget,
            )
            break
        try:
            raw = {}
            counts = {}
            for metric in metric_names:
                counts[metric], raw[metric] = await client.post_metric(
                    post.linkedin_post_urn, metric
                )
            engagement = calculate_engagement_rate(
                counts["IMPRESSION"],
                counts["REACTION"],
                counts["COMMENT"],
                counts["RESHARE"],
                counts["LINK_CLICKS"],
            )
            async with SessionLocal() as db:
                db.add(
                    AnalyticsSnapshot(
                        post_id=post.id,
                        impressions=counts["IMPRESSION"],
                        reactions=counts["REACTION"],
                        comments=counts["COMMENT"],
                        reposts=counts["RESHARE"],
                        clicks=counts["LINK_CLICKS"],
                        engagement_rate=engagement,
                        raw=raw,
                    )
                )
                await db.commit()
        except Exception:
            log.exception("linkedin_analytics_collection_failed", post_id=str(post.id))


async def send_weekly_report() -> None:
    if (
        not settings.linkedin_analytics_enabled
        or not settings.telegram_bot_token.get_secret_value()
        or not settings.telegram_admin_chat_id
    ):
        return
    since = datetime.now(UTC) - timedelta(days=7)
    async with SessionLocal() as db:
        post_count = await db.scalar(
            select(func.count())
            .select_from(Post)
            .where(Post.status == PostStatus.PUBLISHED, Post.published_at >= since)
        )
        latest = (
            select(
                AnalyticsSnapshot.post_id,
                func.max(AnalyticsSnapshot.collected_at).label("collected_at"),
            )
            .group_by(AnalyticsSnapshot.post_id)
            .subquery()
        )
        totals = (
            await db.execute(
                select(
                    func.coalesce(func.sum(AnalyticsSnapshot.impressions), 0),
                    func.coalesce(func.sum(AnalyticsSnapshot.reactions), 0),
                    func.coalesce(func.sum(AnalyticsSnapshot.comments), 0),
                    func.coalesce(func.sum(AnalyticsSnapshot.reposts), 0),
                    func.coalesce(func.sum(AnalyticsSnapshot.clicks), 0),
                )
                .join(
                    latest,
                    (AnalyticsSnapshot.post_id == latest.c.post_id)
                    & (AnalyticsSnapshot.collected_at == latest.c.collected_at),
                )
                .join(Post, Post.id == AnalyticsSnapshot.post_id)
                .where(Post.published_at >= since)
            )
        ).one()
    message = (
        "📊 Haftalık LinkedIn Raporu\n\n"
        "Son 7 günde yayımlanan gönderilerin güncel toplamları\n"
        f"Gönderi: {post_count}\nGösterim: {totals[0]}\nTepki: {totals[1]}\n"
        f"Yorum: {totals[2]}\nRepost: {totals[3]}\nBağlantı tıklaması: {totals[4]}"
    )
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        await bot.initialize()
        await bot.send_message(settings.telegram_admin_chat_id, message)
    finally:
        await bot.shutdown()


async def warn_token_expiry() -> None:
    expires_at = settings.linkedin_access_token_expires_at
    if (
        not expires_at
        or not settings.telegram_admin_chat_id
        or not settings.telegram_bot_token.get_secret_value()
    ):
        return
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at > datetime.now(UTC) + timedelta(days=7):
        return
    marker = expires_at.isoformat()
    async with SessionLocal() as db:
        notified = await db.get(Setting, "linkedin_token_expiry_notified")
        if notified and notified.value.get("expires_at") == marker:
            return
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        await bot.initialize()
        await bot.send_message(
            settings.telegram_admin_chat_id,
            f"⚠️ LinkedIn erişim anahtarı {marker} tarihinde sona eriyor. Yeniden yetkilendirin.",
        )
    finally:
        await bot.shutdown()
    async with SessionLocal() as db:
        await db.execute(
            insert(Setting)
            .values(key="linkedin_token_expiry_notified", value={"expires_at": marker})
            .on_conflict_do_update(
                index_elements=[Setting.key], set_={"value": {"expires_at": marker}}
            )
        )
        await db.commit()
