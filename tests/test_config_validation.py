from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from linkedin_automation.config import Settings


def _production_settings(**overrides):
    values = {
        "app_env": "production",
        "dry_run": False,
        "database_url": "postgresql+asyncpg://app:strong-password@database/app",
        "public_base_url": "https://automation.atasardacagan.com",
        "openai_api_key": "sk-" + ("a" * 40),
        "telegram_bot_token": "123456789:" + ("A" * 35),
        "telegram_admin_chat_id": 123,
        "telegram_admin_user_id": 456,
        "telegram_webhook_secret": "valid_webhook_secret_123456789012345",
        "linkedin_access_token": "test-token-" + ("a" * 32),
        "linkedin_access_token_expires_at": datetime(2030, 1, 1, tzinfo=UTC),
        "linkedin_author_urn": "urn:li:person:abc123",
        "timezone": "Europe/Istanbul",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"public_base_url": "https://your-domain.example"},
            "PUBLIC_BASE_URL must be a real HTTPS deployment URL",
        ),
        (
            {"public_base_url": "https://automation.example.com"},
            "PUBLIC_BASE_URL must be a real HTTPS deployment URL",
        ),
        (
            {
                "database_url": (
                    "postgresql+asyncpg://postgres:postgres@database/linkedin_automation"
                )
            },
            "DATABASE_URL must not use a sample password",
        ),
        (
            {"database_url": "postgresql+asyncpg://app:replace-with-password@database/app"},
            "DATABASE_URL must not use a sample password",
        ),
        (
            {"telegram_webhook_secret": "replace-with-random-webhook-secret"},
            "TELEGRAM_WEBHOOK_SECRET",
        ),
        (
            {"linkedin_author_urn": "urn:li:person:YOUR_ID"},
            "LINKEDIN_AUTHOR_URN must be a person URN",
        ),
        (
            {"linkedin_author_urn": "urn:li:person:abc/def"},
            "LINKEDIN_AUTHOR_URN must be a person URN",
        ),
        (
            {"public_base_url": "https://127.0.0.1"},
            "PUBLIC_BASE_URL must be a real HTTPS deployment URL",
        ),
        (
            {"public_base_url": "https://automation.atasardacagan.com/webhook"},
            "PUBLIC_BASE_URL must be a real HTTPS deployment URL",
        ),
        (
            {"openai_api_key": "not-an-openai-key"},
            "OPENAI_API_KEY must be a non-sample OpenAI secret key",
        ),
        (
            {"telegram_bot_token": "replace-with-bot-token"},
            "TELEGRAM_BOT_TOKEN must be a BotFather token",
        ),
        (
            {"database_url": "sqlite+aiosqlite:///automation.db"},
            "DATABASE_URL must use PostgreSQL with the asyncpg driver",
        ),
        (
            {"database_url": "postgresql+asyncpg://app:@database/app"},
            "DATABASE_URL must include a host, user, password, and database",
        ),
        (
            {"linkedin_access_token": ("a" * 24) + " invalid"},
            "LINKEDIN_ACCESS_TOKEN must be a non-sample member token",
        ),
    ],
)
def test_production_rejects_known_development_sentinels(overrides, message):
    with pytest.raises(ValidationError, match=message):
        _production_settings(**overrides)


def test_default_timezone_is_a_resolvable_iana_timezone():
    config = Settings(_env_file=None)

    assert config.timezone == "Europe/Istanbul"
    assert ZoneInfo(config.timezone).key == config.timezone


def test_valid_production_configuration_preserves_timezone():
    config = _production_settings()

    assert config.timezone == "Europe/Istanbul"


@pytest.mark.parametrize(
    ("field", "value"),
    [("app_env", "prodction"), ("log_level", "VERBOSE")],
)
def test_runtime_mode_and_log_level_reject_typos(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_production_rejects_empty_model_name():
    with pytest.raises(ValidationError, match="OpenAI model names must not be empty"):
        _production_settings(openai_text_model=" ")


def test_bundled_database_password_must_match_connection_url():
    with pytest.raises(ValidationError, match="POSTGRES_PASSWORD must be at least 16 characters"):
        _production_settings(
            postgres_password="different-strong-password",
            database_url="postgresql+asyncpg://postgres:actual-strong-password@db/app",
        )


def test_matching_bundled_database_password_is_accepted():
    password = "matching-strong-password"
    config = _production_settings(
        postgres_password=password,
        database_url=f"postgresql+asyncpg://postgres:{password}@db/app",
    )

    assert config.database_url.endswith("@db/app")
