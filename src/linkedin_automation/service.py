import os
import re
import secrets
import stat
import uuid
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import sqrt
from pathlib import Path

from openai import OpenAIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .ai import ContentEngine
from .config import settings
from .linkedin import (
    AmbiguousLinkedInError,
    LinkedInClient,
    PermanentLinkedInError,
    TransientLinkedInError,
)
from .logging import safe_exception_message
from .models import Approval, AuditEvent, NotificationOutbox, Post, PostStatus, Revision, Setting
from .research import validate_sources


async def audit(db, event, post, actor="system", details=None):
    db.add(
        AuditEvent(
            event=event, post_id=post.id if post else None, actor=actor, details=details or {}
        )
    )


async def _replayed_action(
    db: AsyncSession, event: str, post_id: uuid.UUID, actor: str
) -> Post | None:
    """Return the post when an at-least-once Telegram update already committed."""
    if not actor.startswith("telegram:"):
        return None
    return (
        await db.execute(
            select(Post)
            .join(AuditEvent, AuditEvent.post_id == Post.id)
            .where(
                Post.id == post_id,
                AuditEvent.event == event,
                AuditEvent.actor == actor,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_draft(
    db: AsyncSession, topic: str, sources=None, generation_slot: str | None = None
) -> Post:
    topic = " ".join(topic.split())
    if not topic:
        raise ValueError("Topic cannot be empty")
    if len(topic) > 240:
        raise ValueError("Topic cannot exceed 240 characters")
    if generation_slot:
        if len(generation_slot) > 80:
            raise ValueError("Generation idempotency slot cannot exceed 80 characters")
        existing = await db.scalar(select(Post).where(Post.generation_slot == generation_slot))
        if existing:
            return existing
    recent_types = (
        (await db.execute(select(Post.content_type).order_by(Post.created_at.desc()).limit(3)))
        .scalars()
        .all()
    )
    style_setting = await db.get(Setting, "style_profile")
    style_profile = (
        dict(style_setting.value) if style_setting and isinstance(style_setting.value, dict) else {}
    )
    style_preferences = [
        str(preference)[:240] for preference in style_profile.get("preferences", [])[:20]
    ]
    raw_revision_signals = style_profile.get("revision_signals", {})
    revision_signals = {
        instruction: count
        for instruction, count in (
            raw_revision_signals.items() if isinstance(raw_revision_signals, dict) else []
        )
        if isinstance(instruction, str) and isinstance(count, int)
    }
    recurring_feedback = [
        instruction
        for instruction, count in sorted(
            revision_signals.items(), key=lambda item: item[1], reverse=True
        )
        if count >= 2
    ][:5]
    await db.rollback()
    engine = ContentEngine()
    if sources is None:
        sources = await validate_sources(await engine.research(topic))
    instruction = (
        f"Do not reuse these recent formats in sequence: {recent_types}. "
        f"User style preferences: {style_preferences}. "
        f"Recurring revision feedback to apply: {recurring_feedback}."
    )
    data = await engine.generate(topic, sources, instruction)
    review = await engine.quality_review(data["post"], sources)
    if not review["publishable"] or review["score"] < settings.content_quality_threshold:
        data = await engine.revise(data["post"], review["revision_instruction"], sources)
        review = await engine.quality_review(data["post"], sources)
    if not review["publishable"] or review["score"] < settings.content_quality_threshold:
        raise ValueError("Content did not pass the independent quality gate")
    if data["scores"]["total"] < 65:
        raise ValueError("Topic score is below the publication threshold")
    embedding = await engine.embed(data["post"])
    recent_embeddings = (
        (
            await db.execute(
                select(Post.embedding).where(
                    Post.embedding.is_not(None),
                    Post.created_at >= datetime.now(UTC) - timedelta(days=90),
                )
            )
        )
        .scalars()
        .all()
    )
    await db.rollback()
    if any(_cosine(embedding, old) >= 0.88 for old in recent_embeddings):
        raise ValueError("Draft is too similar to a recent post")
    post_id = uuid.uuid4()
    image_path = None
    image_error_type = None
    if data["needs_image"]:
        try:
            image_path = await engine.image(data["image_prompt"])
        except (OpenAIError, OSError, ValueError) as exc:
            image_error_type = type(exc).__name__
    post = Post(
        id=post_id,
        generation_slot=generation_slot,
        topic=data["topic"],
        category=data["category"],
        content_type=data["content_type"],
        draft=data["post"],
        hashtags=data["hashtags"],
        sources=sources,
        scores={**data["scores"], "quality": review},
        embedding=embedding,
        image_path=image_path,
        status=PostStatus.DRAFT,
        approval_nonce=secrets.token_urlsafe(18),
        approval_expires_at=datetime.now(UTC) + timedelta(hours=48),
    )
    db.add(post)
    await db.flush()
    db.add(Revision(post_id=post.id, version=1, text=post.draft))
    if image_error_type:
        await audit(db, "image_generation_failed", post, details={"error_type": image_error_type})
    post.status = PostStatus.PENDING_APPROVAL
    db.add(
        NotificationOutbox(
            idempotency_key=f"draft:{post.id}:1", post_id=post.id, kind="draft_ready"
        )
    )
    await audit(db, "draft_ready", post)
    await db.commit()
    await db.refresh(post)
    return post


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    denominator = sqrt(sum(a * a for a in left)) * sqrt(sum(b * b for b in right))
    return numerator / denominator if denominator else 0.0


def canonical_commentary(post: Post) -> str:
    hashtags = []
    for raw_tag in post.hashtags:
        tag = "#" + re.sub(r"[^\w]", "", str(raw_tag), flags=re.UNICODE)
        if len(tag) > 1 and tag.casefold() not in {item.casefold() for item in hashtags}:
            hashtags.append(tag)
    commentary = post.draft.strip()
    if hashtags:
        commentary = f"{commentary}\n\n{' '.join(hashtags)}"
    if not commentary or len(commentary) > 3000:
        raise ValueError("LinkedIn commentary must contain 1-3000 characters")
    return commentary


def _content_addressed_image(path: str | None) -> tuple[bytes | None, str | None]:
    if not path:
        return None, None
    image_path = Path(path)
    candidate = image_path if image_path.is_absolute() else Path.cwd() / image_path
    expected_directory = (Path.cwd() / "storage" / "images").resolve()
    error = "Image is not an intact content-addressed PNG artifact"
    try:
        if candidate.parent.resolve(strict=True) != expected_directory:
            raise ValueError(error)
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as file:
            file_stat = os.fstat(file.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or not 8 <= file_stat.st_size <= 30_000_000:
                raise ValueError(error)
            content = file.read(30_000_001)
    except (OSError, ValueError) as exc:
        raise ValueError(error) from exc
    if len(content) > 30_000_000 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(error)
    digest = sha256(content).hexdigest()
    if image_path.suffix.casefold() != ".png" or image_path.stem != digest:
        raise ValueError(error)
    return content, digest


async def approve(db: AsyncSession, post_id: uuid.UUID, nonce: str, actor="telegram") -> Post:
    if replayed := await _replayed_action(db, "human_approved", post_id, actor):
        return replayed
    post = (await db.execute(select(Post).where(Post.id == post_id).with_for_update())).scalar_one()
    if (
        post.status != PostStatus.PENDING_APPROVAL
        or not post.approval_expires_at
        or post.approval_expires_at < datetime.now(UTC)
        or not secrets.compare_digest(post.approval_nonce or "", nonce)
    ):
        raise ValueError("Approval is stale or invalid")
    post.approved_text = canonical_commentary(post)
    post.approved_image_path = post.image_path
    post.approved_at = datetime.now(UTC)
    _, approved_image_digest = _content_addressed_image(post.approved_image_path)
    latest_revision = max(post.revisions, key=lambda revision: revision.version)
    db.add(
        Approval(
            post_id=post.id,
            revision_id=latest_revision.id,
            content_digest=sha256(post.approved_text.encode()).hexdigest(),
            image_digest=approved_image_digest,
            approved_by=actor,
            approved_at=post.approved_at,
        )
    )
    post.status = PostStatus.APPROVED
    post.approval_nonce = None
    post.approval_expires_at = None
    await audit(db, "human_approved", post, actor)
    await db.commit()
    return post


async def schedule_approved(
    db: AsyncSession, post_id: uuid.UUID, scheduled_at: datetime, actor="telegram"
) -> Post:
    if replayed := await _replayed_action(db, "post_scheduled", post_id, actor):
        return replayed
    post = (await db.execute(select(Post).where(Post.id == post_id).with_for_update())).scalar_one()
    if post.status != PostStatus.APPROVED or not post.approved_at or not post.approved_text:
        raise ValueError("Explicit approval required before scheduling")
    if scheduled_at.tzinfo is None:
        raise ValueError("Scheduled time must include a timezone")
    if scheduled_at <= datetime.now(UTC):
        raise ValueError("Scheduled time must be in the future")
    post.scheduled_at = scheduled_at.astimezone(UTC)
    post.status = PostStatus.SCHEDULED
    await audit(db, "post_scheduled", post, actor, {"scheduled_at": post.scheduled_at.isoformat()})
    await db.commit()
    return post


async def revise(
    db: AsyncSession,
    post_id: uuid.UUID,
    instruction: str,
    actor="telegram",
    expected_nonce: str | None = None,
) -> Post:
    if replayed := await _replayed_action(db, "revision_created", post_id, actor):
        return replayed
    instruction = " ".join(instruction.split())
    if not instruction:
        raise ValueError("Revision instruction cannot be empty")
    if len(instruction) > 1000:
        raise ValueError("Revision instruction cannot exceed 1000 characters")
    snapshot = (await db.execute(select(Post).where(Post.id == post_id))).scalar_one()
    nonce = expected_nonce or snapshot.approval_nonce
    if (
        snapshot.status != PostStatus.PENDING_APPROVAL
        or not nonce
        or not secrets.compare_digest(snapshot.approval_nonce or "", nonce)
    ):
        raise ValueError("Post cannot be revised now")
    current_text = snapshot.draft
    current_sources = snapshot.sources
    await db.rollback()

    engine = ContentEngine()
    result = await engine.revise(current_text, instruction, current_sources)
    review = await engine.quality_review(result["post"], current_sources)
    if not review["publishable"] or review["score"] < settings.content_quality_threshold:
        raise ValueError("Revision did not pass the independent quality gate")
    if result["scores"]["total"] < 65:
        raise ValueError("Revision score is below the publication threshold")
    embedding = await engine.embed(result["post"])

    post = (await db.execute(select(Post).where(Post.id == post_id).with_for_update())).scalar_one()
    if post.status != PostStatus.PENDING_APPROVAL or not secrets.compare_digest(
        post.approval_nonce or "", nonce
    ):
        raise ValueError("Post changed while the revision was being generated")
    post.draft = result["post"]
    post.hashtags = result["hashtags"]
    post.category = result["category"]
    post.content_type = result["content_type"]
    post.embedding = embedding
    post.scores = {**result["scores"], "quality": review}
    version = max((revision.version for revision in post.revisions), default=0) + 1
    db.add(Revision(post_id=post.id, version=version, instruction=instruction, text=post.draft))
    post.approval_nonce = secrets.token_urlsafe(18)
    post.approval_expires_at = datetime.now(UTC) + timedelta(hours=48)
    db.add(
        NotificationOutbox(
            idempotency_key=f"draft:{post.id}:{version}",
            post_id=post.id,
            kind="draft_ready",
        )
    )
    style_setting = await db.get(Setting, "style_profile", with_for_update=True)
    style_profile = (
        dict(style_setting.value) if style_setting and isinstance(style_setting.value, dict) else {}
    )
    raw_signals = style_profile.get("revision_signals", {})
    signals = dict(raw_signals) if isinstance(raw_signals, dict) else {}
    signal = instruction[:240]
    previous_count = signals.get(signal, 0)
    signals[signal] = (previous_count if isinstance(previous_count, int) else 0) + 1
    signals = {key: value for key, value in signals.items() if isinstance(value, int)}
    style_profile["revision_signals"] = dict(
        sorted(signals.items(), key=lambda item: item[1], reverse=True)[:50]
    )
    if style_setting:
        style_setting.value = style_profile
    else:
        db.add(Setting(key="style_profile", value=style_profile))
    await audit(db, "revision_created", post, actor, {"version": version})
    await db.commit()
    return post


async def revise_image(
    db: AsyncSession,
    post_id: uuid.UUID,
    instruction: str,
    actor="telegram",
    expected_nonce: str | None = None,
) -> Post:
    if replayed := await _replayed_action(db, "image_revision_created", post_id, actor):
        return replayed
    snapshot = (await db.execute(select(Post).where(Post.id == post_id))).scalar_one()
    nonce = expected_nonce or snapshot.approval_nonce
    if (
        snapshot.status != PostStatus.PENDING_APPROVAL
        or not nonce
        or not secrets.compare_digest(snapshot.approval_nonce or "", nonce)
    ):
        raise ValueError("Post image cannot be revised now")
    await db.rollback()

    image_path = await ContentEngine().image(instruction)

    post = (await db.execute(select(Post).where(Post.id == post_id).with_for_update())).scalar_one()
    if post.status != PostStatus.PENDING_APPROVAL or not secrets.compare_digest(
        post.approval_nonce or "", nonce
    ):
        raise ValueError("Post changed while the image was being generated")
    version = max((revision.version for revision in post.revisions), default=0) + 1
    post.image_path = image_path
    db.add(
        Revision(
            post_id=post.id,
            version=version,
            instruction=f"Image: {instruction}",
            text=post.draft,
            image_path=post.image_path,
        )
    )
    post.approval_nonce = secrets.token_urlsafe(18)
    post.approval_expires_at = datetime.now(UTC) + timedelta(hours=48)
    db.add(
        NotificationOutbox(
            idempotency_key=f"draft:{post.id}:{version}",
            post_id=post.id,
            kind="draft_ready",
        )
    )
    await audit(db, "image_revision_created", post, actor, {"version": version})
    await db.commit()
    return post


async def reject(db: AsyncSession, post_id: uuid.UUID, nonce: str, actor: str = "telegram") -> Post:
    if replayed := await _replayed_action(db, "human_rejected", post_id, actor):
        return replayed
    post = (await db.execute(select(Post).where(Post.id == post_id).with_for_update())).scalar_one()
    if post.status != PostStatus.PENDING_APPROVAL or not secrets.compare_digest(
        post.approval_nonce or "", nonce
    ):
        raise ValueError("Reject action is stale or invalid")
    post.status = PostStatus.REJECTED
    post.approval_nonce = None
    post.approval_expires_at = None
    await audit(db, "human_rejected", post, actor)
    await db.commit()
    return post


async def publish_approved(
    db: AsyncSession,
    post_id: uuid.UUID,
    *,
    scheduled_worker: bool = False,
    actor: str = "system",
) -> Post:
    if replayed := await _replayed_action(db, "linkedin_publish_dry_run_completed", post_id, actor):
        return replayed
    post = (
        await db.execute(select(Post).where(Post.id == post_id).with_for_update(skip_locked=True))
    ).scalar_one_or_none()
    if not post:
        raise ValueError("Post unavailable")
    if post.status == PostStatus.PUBLISHED:
        return post
    allowed_statuses = {PostStatus.APPROVED}
    if scheduled_worker:
        allowed_statuses.update({PostStatus.SCHEDULED, PostStatus.PUBLISH_FAILED})
    if (
        post.status not in allowed_statuses
        or not post.approved_at
        or not post.approved_text
        or post.requires_reconciliation
        or (post.status == PostStatus.PUBLISH_FAILED and post.publish_attempts >= 5)
    ):
        raise ValueError("Explicit approval required")
    if post.status == PostStatus.SCHEDULED and (
        not post.scheduled_at or post.scheduled_at > datetime.now(UTC)
    ):
        raise ValueError("Scheduled publication is not due")
    if post.status == PostStatus.PUBLISH_FAILED and (
        not post.next_publish_attempt_at or post.next_publish_attempt_at > datetime.now(UTC)
    ):
        raise ValueError("Publication retry is not due")
    approval = (
        await db.execute(select(Approval).where(Approval.post_id == post.id))
    ).scalar_one_or_none()
    content_digest = sha256(post.approved_text.encode()).hexdigest()
    try:
        image_content, image_digest = _content_addressed_image(post.approved_image_path)
    except (OSError, ValueError) as exc:
        post.status = PostStatus.PUBLISH_FAILED
        post.next_publish_attempt_at = None
        post.last_error = f"{type(exc).__name__}: approved image integrity failure"
        db.add(
            NotificationOutbox(
                idempotency_key=f"integrity-failed:{post.id}",
                post_id=post.id,
                kind="publish_integrity_failure",
            )
        )
        await audit(
            db,
            "approved_artifact_integrity_failed",
            post,
            details={"error_type": type(exc).__name__},
        )
        await db.commit()
        raise ValueError("Approved image artifact is missing or has changed") from exc
    if (
        not approval
        or not secrets.compare_digest(approval.content_digest, content_digest)
        or approval.image_digest != image_digest
    ):
        post.status = PostStatus.PUBLISH_FAILED
        post.next_publish_attempt_at = None
        post.last_error = "Approved artifact integrity check failed"
        db.add(
            NotificationOutbox(
                idempotency_key=f"integrity-failed:{post.id}",
                post_id=post.id,
                kind="publish_integrity_failure",
            )
        )
        await audit(db, "approved_artifact_integrity_failed", post)
        await db.commit()
        raise ValueError("Approved artifact integrity check failed")
    if settings.dry_run:
        await LinkedInClient().publish(post.approved_text, image_content)
        marker = int(datetime.now(UTC).timestamp() * 1000)
        db.add(
            NotificationOutbox(
                idempotency_key=f"dry-run:{post.id}:{marker}",
                post_id=post.id,
                kind="dry_run_completed",
            )
        )
        post.scheduled_at = None
        post.status = PostStatus.APPROVED
        await audit(db, "linkedin_publish_dry_run_completed", post, actor)
        await db.commit()
        return post
    post.status = PostStatus.PUBLISHING
    post.publish_attempts += 1
    await db.commit()
    try:
        urn, url = await LinkedInClient().publish(post.approved_text, image_content)
        await db.refresh(post, with_for_update=True)
        if post.status == PostStatus.PUBLISHED:
            return post
        post.linkedin_post_urn = urn
        post.linkedin_url = url
        post.last_error = None
        post.next_publish_attempt_at = None
        post.published_at = datetime.now(UTC)
        post.status = PostStatus.PUBLISHED
        post.requires_reconciliation = False
        db.add(
            NotificationOutbox(
                idempotency_key=f"published:{post.id}",
                post_id=post.id,
                kind="publish_succeeded",
            )
        )
        await audit(db, "linkedin_published", post, actor)
    except AmbiguousLinkedInError as exc:
        await db.refresh(post, with_for_update=True)
        if post.status == PostStatus.PUBLISHED:
            return post
        post.status = PostStatus.PUBLISHING
        post.requires_reconciliation = True
        post.last_error = safe_exception_message(exc)
        db.add(
            NotificationOutbox(
                idempotency_key=f"reconcile:{post.id}:{post.publish_attempts}",
                post_id=post.id,
                kind="publish_reconciliation_required",
            )
        )
        await audit(
            db,
            "linkedin_publish_requires_reconciliation",
            post,
            details={"error_type": type(exc).__name__},
        )
        await db.commit()
        raise
    except TransientLinkedInError as exc:
        await db.refresh(post, with_for_update=True)
        if post.status == PostStatus.PUBLISHED:
            return post
        post.status = PostStatus.PUBLISH_FAILED
        post.requires_reconciliation = False
        post.last_error = safe_exception_message(exc)
        delay_minutes = min(60, 2 ** max(post.publish_attempts - 1, 0))
        post.next_publish_attempt_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
        db.add(
            NotificationOutbox(
                idempotency_key=f"publish-failed:{post.id}:{post.publish_attempts}",
                post_id=post.id,
                kind="publish_retry_scheduled",
            )
        )
        await audit(db, "linkedin_publish_failed", post, details={"error_type": type(exc).__name__})
        await db.commit()
        raise
    except (PermanentLinkedInError, OSError, ValueError) as exc:
        await db.refresh(post, with_for_update=True)
        if post.status == PostStatus.PUBLISHED:
            return post
        post.status = PostStatus.PUBLISH_FAILED
        post.requires_reconciliation = False
        post.last_error = safe_exception_message(exc)
        post.next_publish_attempt_at = None
        db.add(
            NotificationOutbox(
                idempotency_key=f"publish-permanent:{post.id}:{post.publish_attempts}",
                post_id=post.id,
                kind="publish_permanent_failure",
            )
        )
        await audit(
            db,
            "linkedin_publish_permanent_failure",
            post,
            details={"error_type": type(exc).__name__},
        )
        await db.commit()
        raise
    except Exception as exc:
        await db.refresh(post, with_for_update=True)
        if post.status == PostStatus.PUBLISHED:
            return post
        post.status = PostStatus.PUBLISHING
        post.requires_reconciliation = True
        post.last_error = safe_exception_message(exc)
        db.add(
            NotificationOutbox(
                idempotency_key=f"publish-unknown:{post.id}:{post.publish_attempts}",
                post_id=post.id,
                kind="publish_reconciliation_required",
            )
        )
        await audit(
            db,
            "linkedin_publish_unknown_outcome",
            post,
            details={"error_type": type(exc).__name__},
        )
        await db.commit()
        raise
    await db.commit()
    return post


async def reconcile_publication(
    db: AsyncSession,
    post_id: uuid.UUID,
    resolution: str,
    *,
    linkedin_urn: str | None = None,
    actor: str = "telegram",
) -> Post:
    replay_event = (
        "linkedin_publish_reconciled_as_published"
        if resolution == "published"
        else "linkedin_publish_reconciled_as_not_published"
    )
    if replayed := await _replayed_action(db, replay_event, post_id, actor):
        return replayed
    post = (
        await db.execute(select(Post).where(Post.id == post_id).with_for_update())
    ).scalar_one_or_none()
    if not post or not post.requires_reconciliation or post.status != PostStatus.PUBLISHING:
        raise ValueError("Post is not awaiting publication reconciliation")
    if resolution == "published":
        if not linkedin_urn or not re.fullmatch(
            r"urn:li:(?:share|ugcPost):[A-Za-z0-9_-]{1,200}", linkedin_urn
        ):
            raise ValueError("A valid LinkedIn share or ugcPost URN is required")
        post.linkedin_post_urn = linkedin_urn
        post.linkedin_url = f"https://www.linkedin.com/feed/update/{linkedin_urn}"
        post.published_at = datetime.now(UTC)
        post.status = PostStatus.PUBLISHED
        post.requires_reconciliation = False
        post.last_error = None
        existing_notice = await db.scalar(
            select(NotificationOutbox.id).where(
                NotificationOutbox.idempotency_key == f"published:{post.id}"
            )
        )
        if not existing_notice:
            db.add(
                NotificationOutbox(
                    idempotency_key=f"published:{post.id}",
                    post_id=post.id,
                    kind="publish_succeeded",
                )
            )
        await audit(db, "linkedin_publish_reconciled_as_published", post, actor)
    elif resolution == "not-published":
        post.status = PostStatus.APPROVED
        post.requires_reconciliation = False
        post.next_publish_attempt_at = None
        post.last_error = None
        await audit(db, "linkedin_publish_reconciled_as_not_published", post, actor)
    else:
        raise ValueError("Resolution must be published or not-published")
    await db.commit()
    return post
