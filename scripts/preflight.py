from pydantic import ValidationError

from linkedin_automation.config import Settings


def main() -> None:
    try:
        config = Settings()
    except ValidationError as exc:
        messages = "; ".join(error["msg"] for error in exc.errors())
        raise SystemExit(f"Configuration is not ready: {messages}") from exc
    print(
        "Configuration is valid: "
        f"environment={config.app_env}, dry_run={config.dry_run}, timezone={config.timezone}, "
        f"linkedin_version={config.linkedin_version}"
    )


if __name__ == "__main__":
    main()
