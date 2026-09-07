import logging

from linkedin_automation.logging import SecretRedactionFilter, redact, safe_exception_message


def test_standard_logging_filter_redacts_credentials():
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "POST https://api.telegram.org/bot123456:ABC/getMe Authorization: Bearer topsecret",
        (),
        None,
    )
    SecretRedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "123456:ABC" not in rendered
    assert "topsecret" not in rendered


def test_persisted_exception_message_redacts_key_and_database_password():
    openai_key = "sk-" + ("x" * 32)
    message = safe_exception_message(
        RuntimeError(
            f"request failed for {openai_key} at postgresql+asyncpg://app:database-secret@db/app"
        )
    )

    assert openai_key not in message
    assert "database-secret" not in message
    assert "[REDACTED" in message


def test_structured_redaction_recurses_into_nested_values():
    event = redact(
        None,
        None,
        {
            "payload": {"authorization": "Bearer hidden", "items": ["token=secret"]},
            "webhook_secret": "hidden",
        },
    )

    assert "hidden" not in str(event)
    assert "secret" not in str(event["payload"])
