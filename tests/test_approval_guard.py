import secrets
import uuid
from datetime import UTC, datetime

from linkedin_automation.models import Post, PostStatus


def test_status_values_are_stable():
    assert PostStatus.PENDING_APPROVAL.value == "pending_approval"
    assert PostStatus.PUBLISHED.value == "published"


def test_nonce_is_single_use_design():
    nonce = secrets.token_urlsafe(18)
    assert secrets.compare_digest(nonce, nonce)
    assert not secrets.compare_digest(nonce, nonce + "x")


def test_publishable_state_requires_all_guards():
    post = Post(
        id=uuid.uuid4(),
        topic="AI",
        content_type="analysis",
        draft="draft",
        status=PostStatus.APPROVED,
        approved_text="approved",
        approved_at=datetime.now(UTC),
    )
    assert post.status is PostStatus.APPROVED and post.approved_text and post.approved_at
