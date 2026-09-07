from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from linkedin_automation import telegram


class _FakeSession:
    def __init__(self, value):
        self.row = SimpleNamespace(value=value) if value is not None else None

    async def get(self, _model, key):
        assert key == "content_calendar"
        return self.row


class _FakeSessionContext:
    def __init__(self, value):
        self.session = _FakeSession(value)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def test_clean_topics_normalizes_deduplicates_and_enforces_length():
    topics = telegram._clean_topics(
        [
            "  Data    Science  ",
            "data science",
            "\tMobility\n",
            "",
            " " * 4,
            "x" * 161,
        ]
    )

    assert topics == ["Data Science", "Mobility"]


def test_frequency_uses_explicit_matching_weekdays():
    assert telegram._weekdays_for_frequency(2, [4, 1]) == [1, 4]
    assert telegram._weekdays_for_frequency(2, [0, 3]) == [0, 3]


def test_frequency_falls_back_to_balanced_days_for_invalid_combination():
    assert telegram._weekdays_for_frequency(3, [0]) == [0, 2, 4]
    with pytest.raises(ValueError):
        telegram._weekdays_for_frequency(8, [])


@pytest.mark.asyncio
async def test_current_calendar_uses_safe_defaults_for_invalid_values(monkeypatch):
    invalid = {"weekdays": [-1, 7], "hour": 24, "minute": -1, "skip_until": "future"}
    monkeypatch.setattr(telegram, "SessionLocal", lambda: _FakeSessionContext(invalid))

    calendar = await telegram._current_calendar()

    assert calendar["weekdays"] == telegram.CALENDAR_DEFAULTS["weekdays"]
    assert calendar["hour"] == telegram.CALENDAR_DEFAULTS["hour"]
    assert calendar["minute"] == telegram.CALENDAR_DEFAULTS["minute"]
    assert calendar["skip_until"] == "future"


@pytest.mark.asyncio
async def test_current_calendar_does_not_accept_booleans_as_numbers(monkeypatch):
    invalid = {"weekdays": [True], "hour": True, "minute": False}
    monkeypatch.setattr(telegram, "SessionLocal", lambda: _FakeSessionContext(invalid))

    calendar = await telegram._current_calendar()

    assert calendar["weekdays"] == telegram.CALENDAR_DEFAULTS["weekdays"]
    assert calendar["hour"] == telegram.CALENDAR_DEFAULTS["hour"]
    assert calendar["minute"] == telegram.CALENDAR_DEFAULTS["minute"]


@pytest.mark.asyncio
async def test_current_calendar_preserves_valid_custom_values(monkeypatch):
    custom = {"weekdays": [1, 3], "hour": 18, "minute": 45}
    monkeypatch.setattr(telegram, "SessionLocal", lambda: _FakeSessionContext(custom))

    assert await telegram._current_calendar() == custom


def test_skip_deadline_preserves_turkish_quantities():
    now = datetime(2026, 9, 6, 12, 0, tzinfo=ZoneInfo("Europe/Istanbul"))

    assert telegram._parse_skip_deadline("2 hafta", now) == datetime(
        2026, 9, 20, 12, 0, tzinfo=ZoneInfo("Europe/Istanbul")
    )
    assert telegram._parse_skip_deadline("3 ay", now) == datetime(
        2026, 12, 6, 12, 0, tzinfo=ZoneInfo("Europe/Istanbul")
    )


def test_next_week_phrase_means_next_monday_midnight():
    now = datetime(2026, 9, 6, 12, 0, tzinfo=ZoneInfo("Europe/Istanbul"))

    assert telegram._parse_skip_deadline("gelecek haftaya kadar", now) == datetime(
        2026, 9, 7, 0, 0, tzinfo=ZoneInfo("Europe/Istanbul")
    )


@pytest.mark.asyncio
async def test_error_handler_marks_update_for_durable_retry_and_notifies_once(monkeypatch):
    bot_data = {}
    send_message = AsyncMock()
    context = SimpleNamespace(
        error=RuntimeError("temporary failure"),
        application=SimpleNamespace(bot_data=bot_data),
        bot=SimpleNamespace(send_message=send_message),
    )
    update = SimpleNamespace(update_id=42)
    monkeypatch.setattr(telegram.settings, "telegram_admin_chat_id", 123)

    await telegram.error_handler(update, context)
    await telegram.error_handler(update, context)

    assert bot_data["_failed_updates"] == {42: "RuntimeError"}
    assert bot_data["_failed_update_notified"] == {42}
    send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_propagates_unexpected_failure_to_durable_inbox(monkeypatch):
    monkeypatch.setattr(telegram.settings, "telegram_admin_chat_id", 123)
    monkeypatch.setattr(telegram.settings, "telegram_admin_user_id", 456)
    query = SimpleNamespace(
        data=None,
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=123, type="private"),
        effective_user=SimpleNamespace(id=456),
        callback_query=query,
        update_id=42,
    )

    with pytest.raises(AttributeError):
        await telegram.callback(update, SimpleNamespace(bot=SimpleNamespace()))


@pytest.mark.asyncio
async def test_keyboard_cleanup_failure_does_not_undo_committed_action():
    query = SimpleNamespace(edit_message_reply_markup=AsyncMock(side_effect=RuntimeError("old")))

    await telegram._remove_inline_keyboard(query)

    query.edit_message_reply_markup.assert_awaited_once_with(None)
