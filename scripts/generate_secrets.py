import secrets


def main() -> None:
    print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}")
    print(f"TELEGRAM_WEBHOOK_SECRET={secrets.token_urlsafe(32)}")


if __name__ == "__main__":
    main()
