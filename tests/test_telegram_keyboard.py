import secrets
import uuid

from linkedin_automation.models import Post, PostStatus
from linkedin_automation.telegram import review_keyboard


def test_all_review_callbacks_fit_telegram_limit():
    post = Post(
        id=uuid.uuid4(),
        topic="AI",
        content_type="analysis",
        draft="Draft",
        status=PostStatus.PENDING_APPROVAL,
        approval_nonce=secrets.token_urlsafe(18),
    )
    keyboard = review_keyboard(post)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks
    assert all(len(value.encode()) <= 64 for value in callbacks)
