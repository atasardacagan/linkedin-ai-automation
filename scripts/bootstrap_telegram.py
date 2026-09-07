import os

import httpx
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is empty in .env")
    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"limit": 100, "timeout": 0},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise SystemExit(f"Telegram could not be reached: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise SystemExit(
            f"Telegram returned HTTP {response.status_code}. Check the token and remove any "
            "existing webhook before using getUpdates."
        )
    payload = response.json()
    if not payload.get("ok"):
        raise SystemExit("Telegram rejected the request. Check the bot token.")
    matches = []
    for update in payload.get("result", []):
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") == "private" and isinstance(sender.get("id"), int):
            matches.append((update.get("update_id", 0), chat.get("id"), sender.get("id")))
    if not matches:
        raise SystemExit(
            "No private message was found. Open the bot in Telegram, press Start, send /start, "
            "then run this helper again."
        )
    _, chat_id, user_id = max(matches)
    print(f"TELEGRAM_ADMIN_CHAT_ID={chat_id}")
    print(f"TELEGRAM_ADMIN_USER_ID={user_id}")


if __name__ == "__main__":
    main()
