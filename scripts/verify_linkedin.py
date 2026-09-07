import os
import re

import httpx
from dotenv import load_dotenv


def _member_id(client: httpx.Client, headers: dict[str, str]) -> str | None:
    for endpoint, field in (("/v2/me", "id"), ("/v2/userinfo", "sub")):
        try:
            response = client.get(endpoint, headers=headers)
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue
        value = response.json().get(field)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
            return value
    return None


def main() -> None:
    load_dotenv()
    token = os.getenv("LINKEDIN_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit("LINKEDIN_ACCESS_TOKEN is empty in .env")
    headers = {"Authorization": f"Bearer {token}", "X-Restli-Protocol-Version": "2.0.0"}
    with httpx.Client(base_url="https://api.linkedin.com", timeout=30) as client:
        member_id = _member_id(client, headers)
    if not member_id:
        raise SystemExit(
            "The token is not authorized for /v2/me or /v2/userinfo. Add the OpenID Connect "
            "product/scopes or copy the authenticated member ID from LinkedIn Token Inspector."
        )
    print(f"LINKEDIN_AUTHOR_URN=urn:li:person:{member_id}")


if __name__ == "__main__":
    main()
