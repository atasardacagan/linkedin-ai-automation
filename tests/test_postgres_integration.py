import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from linkedin_automation.db import SessionLocal, engine
from linkedin_automation.models import Post, PostStatus, Revision
from linkedin_automation.service import (
    approve,
    publish_approved,
    reconcile_publication,
    schedule_approved,
)


@pytest.fixture(scope="module", autouse=True)
async def postgres_is_available():
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1 FROM posts LIMIT 1"))
    except (OSError, SQLAlchemyError) as exc:
        pytest.skip(f"PostgreSQL integration database is unavailable: {type(exc).__name__}")


async def _pending_post() -> tuple[uuid.UUID, str]:
    post_id = uuid.uuid4()
    nonce = secrets.token_urlsafe(18)
    async with SessionLocal() as db:
        db.add(
            Post(
                id=post_id,
                topic="Integration test",
                content_type="analysis",
                draft="Exact integration test commentary",
                hashtags=["test"],
                sources=[],
                scores={},
                status=PostStatus.PENDING_APPROVAL,
                approval_nonce=nonce,
                approval_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.add(Revision(post_id=post_id, version=1, text="Exact integration test commentary"))
        await db.commit()
    return post_id, nonce


@pytest.mark.asyncio
async def test_expired_approval_nonce_is_rejected():
    post_id, nonce = await _pending_post()
    async with SessionLocal() as db:
        post = await db.get(Post, post_id, with_for_update=True)
        post.approval_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="stale or invalid"):
            await approve(db, post_id, nonce)


@pytest.mark.asyncio
async def test_replayed_telegram_approval_returns_committed_result():
    post_id, nonce = await _pending_post()
    actor = "telegram:user:123:chat:123:update:456"
    async with SessionLocal() as db:
        first = await approve(db, post_id, nonce, actor=actor)
    async with SessionLocal() as db:
        replayed = await approve(db, post_id, nonce, actor=actor)

    assert first.id == replayed.id == post_id
    assert replayed.status == PostStatus.APPROVED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [PostStatus.PENDING_APPROVAL, PostStatus.SCHEDULED])
async def test_database_rejects_incomplete_guarded_states(status):
    post = Post(
        id=uuid.uuid4(),
        topic="Invalid guarded state",
        content_type="analysis",
        draft="Draft",
        hashtags=[],
        sources=[],
        scores={},
        status=status,
        approved_at=datetime.now(UTC) if status == PostStatus.SCHEDULED else None,
        approved_text="Approved" if status == PostStatus.SCHEDULED else None,
    )
    async with SessionLocal() as db:
        db.add(post)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


@pytest.mark.asyncio
async def test_scheduled_approved_post_cannot_publish_before_due_time():
    post_id, nonce = await _pending_post()
    async with SessionLocal() as db:
        await approve(db, post_id, nonce, actor="integration-test")
    async with SessionLocal() as db:
        await schedule_approved(db, post_id, datetime.now(UTC) + timedelta(hours=1))
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="not due"):
            await publish_approved(db, post_id, scheduled_worker=True)


@pytest.mark.asyncio
async def test_approved_commentary_tampering_is_stopped_durably():
    post_id, nonce = await _pending_post()
    async with SessionLocal() as db:
        await approve(db, post_id, nonce, actor="integration-test")
    async with SessionLocal() as db:
        post = await db.get(Post, post_id, with_for_update=True)
        post.approved_text = "Tampered commentary"
        await db.commit()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="integrity"):
            await publish_approved(db, post_id)
    async with SessionLocal() as db:
        post = await db.get(Post, post_id)
        assert post.status == PostStatus.PUBLISH_FAILED
        assert post.next_publish_attempt_at is None


@pytest.mark.asyncio
async def test_not_published_reconciliation_returns_to_approved_without_republishing():
    post_id, nonce = await _pending_post()
    async with SessionLocal() as db:
        await approve(db, post_id, nonce, actor="integration-test")
    async with SessionLocal() as db:
        post = await db.get(Post, post_id, with_for_update=True)
        post.status = PostStatus.PUBLISHING
        post.requires_reconciliation = True
        await db.commit()
    async with SessionLocal() as db:
        post = await reconcile_publication(db, post_id, "not-published")

    assert post.status == PostStatus.APPROVED
    assert post.requires_reconciliation is False
    assert post.linkedin_post_urn is None
