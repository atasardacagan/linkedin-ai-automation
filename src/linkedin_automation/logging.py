import logging
import re
from urllib.parse import urlparse

import structlog

from .config import settings

_SECRET = re.compile(
    r"(?i)(bearer\s+|api[_-]?key[=:]\s*|token[=:]\s*|api\.telegram\.org/bot)[^\s,/]+"
)
_OPENAI_KEY = re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b")
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,20}:[A-Za-z0-9_-]{30,}\b")
_URL_PASSWORD = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)")


def redact_text(value: object) -> str:
    text = str(value)
    text = _SECRET.sub(r"\1[REDACTED]", text)
    text = _OPENAI_KEY.sub("[REDACTED_OPENAI_KEY]", text)
    text = _TELEGRAM_TOKEN.sub("[REDACTED_TELEGRAM_TOKEN]", text)
    text = _URL_PASSWORD.sub(r"\1[REDACTED]\2", text)
    configured_secrets = (
        settings.openai_api_key.get_secret_value(),
        settings.telegram_bot_token.get_secret_value(),
        settings.telegram_webhook_secret.get_secret_value(),
        settings.linkedin_access_token.get_secret_value(),
        settings.postgres_password.get_secret_value(),
        urlparse(settings.database_url).password or "",
    )
    for secret in configured_secrets:
        if len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    return text


def safe_exception_message(exc: Exception, limit: int = 500) -> str:
    message = " ".join(redact_text(exc).split())
    return f"{type(exc).__name__}: {message[:limit]}"


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: redact_text(value) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(redact_text(value) for value in record.args)
        return True


def _redact_value(value):
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if any(
                    marker in str(key).lower()
                    for marker in ("secret", "token", "password", "api_key")
                )
                else _redact_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return redact_text(value) if isinstance(value, str) else value


def redact(_, __, event_dict):
    for key, value in list(event_dict.items()):
        if any(x in key.lower() for x in ("secret", "token", "password", "api_key")):
            event_dict[key] = "[REDACTED]"
        else:
            event_dict[key] = _redact_value(value)
    return event_dict


def configure_logging(level="INFO"):
    logging.basicConfig(level=level, format="%(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretRedactionFilter())
    # Third-party clients may include Telegram's bot token in request URLs.
    for noisy_logger in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.format_exc_info,
            redact,
            structlog.processors.JSONRenderer(),
        ]
    )
