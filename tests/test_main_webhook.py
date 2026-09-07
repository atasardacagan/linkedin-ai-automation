from linkedin_automation import main


def _message_payload(user_id=456, chat_id=123, chat_type="private"):
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": chat_id, "type": chat_type},
            "text": "/status",
        },
    }


def test_webhook_payload_filter_accepts_only_configured_private_admin(monkeypatch):
    monkeypatch.setattr(main.settings, "telegram_admin_chat_id", 123)
    monkeypatch.setattr(main.settings, "telegram_admin_user_id", 456)

    assert main._is_admin_payload(_message_payload()) is True
    assert main._is_admin_payload(_message_payload(user_id=999)) is False
    assert main._is_admin_payload(_message_payload(chat_id=999)) is False
    assert main._is_admin_payload(_message_payload(chat_type="group")) is False


def test_webhook_payload_filter_accepts_admin_callback(monkeypatch):
    monkeypatch.setattr(main.settings, "telegram_admin_chat_id", 123)
    monkeypatch.setattr(main.settings, "telegram_admin_user_id", 456)
    payload = {
        "update_id": 2,
        "callback_query": {
            "from": {"id": 456},
            "message": {"chat": {"id": 123, "type": "private"}},
            "data": "p:00000000-0000-0000-0000-000000000000",
        },
    }

    assert main._is_admin_payload(payload) is True


def test_webhook_payload_filter_rejects_boolean_and_unknown_ids(monkeypatch):
    monkeypatch.setattr(main.settings, "telegram_admin_chat_id", 1)
    monkeypatch.setattr(main.settings, "telegram_admin_user_id", 1)

    assert main._is_admin_payload(_message_payload(user_id=True, chat_id=True)) is False
    assert main._is_admin_payload({"update_id": 3, "edited_message": {}}) is False
