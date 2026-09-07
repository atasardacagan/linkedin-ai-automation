import ipaddress
import re
import secrets
from datetime import UTC, datetime
from functools import lru_cache
from typing import Literal
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    postgres_password: SecretStr = SecretStr("")
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/linkedin_automation"
    public_base_url: str = "http://localhost:8000"
    openai_api_key: SecretStr = SecretStr("")
    openai_text_model: str = "gpt-5.6-terra"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_image_model: str = "gpt-image-2"
    content_quality_threshold: int = Field(default=75, ge=0, le=100)
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_admin_chat_id: int | None = None
    telegram_admin_user_id: int | None = None
    telegram_webhook_secret: SecretStr = SecretStr("")
    linkedin_access_token: SecretStr = SecretStr("")
    linkedin_access_token_expires_at: datetime | None = None
    linkedin_author_urn: str = ""
    linkedin_version: str = Field(default="202608", pattern=r"^\d{6}$")
    linkedin_analytics_enabled: bool = False
    linkedin_analytics_daily_call_budget: int = Field(default=80, ge=5, le=1000)
    linkedin_analytics_lookback_days: int = Field(default=90, ge=1, le=365)
    timezone: str = "Europe/Istanbul"
    auto_generation_enabled: bool = True
    dry_run: bool = True

    @model_validator(mode="after")
    def validate_safe_runtime(self):
        strict = self.app_env == "production" or not self.dry_run
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("TIMEZONE must be a valid IANA timezone") from exc
        if not strict:
            return self
        errors = []
        public_url = urlparse(self.public_base_url)
        if (
            public_url.scheme != "https"
            or not public_url.hostname
            or public_url.username
            or public_url.password
            or public_url.path not in {"", "/"}
            or public_url.query
            or public_url.fragment
            or _is_reserved_hostname(public_url.hostname)
        ) or _is_non_public_ip(public_url.hostname):
            errors.append("PUBLIC_BASE_URL must be a real HTTPS deployment URL")
        openai_key = self.openai_api_key.get_secret_value()
        if len(openai_key) < 20 or not openai_key.startswith("sk-"):
            errors.append("OPENAI_API_KEY must be a non-sample OpenAI secret key")
        if not all(
            model.strip()
            for model in (
                self.openai_text_model,
                self.openai_embedding_model,
                self.openai_image_model,
            )
        ):
            errors.append("OpenAI model names must not be empty")
        telegram_token = self.telegram_bot_token.get_secret_value()
        if not re.fullmatch(r"\d{6,20}:[A-Za-z0-9_-]{30,}", telegram_token):
            errors.append("TELEGRAM_BOT_TOKEN must be a BotFather token")
        if (
            self.telegram_admin_chat_id is None
            or self.telegram_admin_chat_id <= 0
            or self.telegram_admin_user_id is None
            or self.telegram_admin_user_id <= 0
        ):
            errors.append("TELEGRAM_ADMIN_CHAT_ID and TELEGRAM_ADMIN_USER_ID must be positive")
        webhook_secret = self.telegram_webhook_secret.get_secret_value()
        if (
            len(webhook_secret) < 32
            or "replace-with" in webhook_secret
            or len(webhook_secret) > 256
            or not all(character.isalnum() or character in "_-" for character in webhook_secret)
        ):
            errors.append(
                "TELEGRAM_WEBHOOK_SECRET must contain 32-256 A-Z, a-z, 0-9, _ or - characters"
            )
        linkedin_token = self.linkedin_access_token.get_secret_value()
        if (
            len(linkedin_token) < 20
            or any(character.isspace() for character in linkedin_token)
            or any(marker in linkedin_token.casefold() for marker in ("replace-with", "your-token"))
        ):
            errors.append("LINKEDIN_ACCESS_TOKEN must be a non-sample member token")
        if self.linkedin_access_token_expires_at is None:
            errors.append("LINKEDIN_ACCESS_TOKEN_EXPIRES_AT is required")
        else:
            expires_at = self.linkedin_access_token_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                errors.append("LINKEDIN_ACCESS_TOKEN_EXPIRES_AT must be in the future")
        if not re.fullmatch(r"urn:li:person:[A-Za-z0-9_-]{1,200}", self.linkedin_author_urn) or any(
            marker in self.linkedin_author_urn.casefold()
            for marker in ("your_id", "replace-with", "example")
        ):
            errors.append("LINKEDIN_AUTHOR_URN must be a person URN")
        database_url = self.database_url.casefold()
        if not database_url.startswith("postgresql+asyncpg://"):
            errors.append("DATABASE_URL must use PostgreSQL with the asyncpg driver")
        parsed_database = urlparse(self.database_url)
        if (
            not parsed_database.hostname
            or not parsed_database.username
            or not parsed_database.password
            or parsed_database.path in {"", "/"}
        ):
            errors.append("DATABASE_URL must include a host, user, password, and database")
        if any(
            marker in database_url
            for marker in ("postgres:postgres", "replace-with", "paste_the_same")
        ):
            errors.append("DATABASE_URL must not use a sample password")
        if parsed_database.hostname and parsed_database.hostname.casefold() == "db":
            compose_password = self.postgres_password.get_secret_value()
            database_password = unquote(parsed_database.password or "")
            if len(compose_password) < 16 or not secrets.compare_digest(
                compose_password, database_password
            ):
                errors.append(
                    "POSTGRES_PASSWORD must be at least 16 characters and match DATABASE_URL "
                    "when using the bundled db service"
                )
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _is_non_public_ip(hostname: str) -> bool:
    try:
        return not ipaddress.ip_address(hostname).is_global
    except ValueError:
        return False


def _is_reserved_hostname(hostname: str) -> bool:
    hostname = hostname.casefold().rstrip(".")
    reserved_domains = {
        "example.com",
        "example.net",
        "example.org",
        "localhost",
    }
    reserved_suffixes = (".example", ".invalid", ".local", ".localhost", ".test", ".internal")
    return (
        hostname in reserved_domains
        or any(hostname.endswith(f".{domain}") for domain in reserved_domains)
        or hostname.endswith(reserved_suffixes)
        or "your-domain" in hostname
    )


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
