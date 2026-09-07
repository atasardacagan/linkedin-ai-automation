import re
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from .config import settings


class TransientLinkedInError(RuntimeError):
    pass


class AmbiguousLinkedInError(RuntimeError):
    pass


class PermanentLinkedInError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class LinkedInClient:
    def __init__(self):
        self.base = "https://api.linkedin.com"
        self.headers = {
            "Authorization": f"Bearer {settings.linkedin_access_token.get_secret_value()}",
            "LinkedIn-Version": settings.linkedin_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    async def _request(self, method, path, *, ambiguous_on_transport=False, **kwargs):
        try:
            async with httpx.AsyncClient(
                base_url=self.base, headers=self.headers, timeout=45
            ) as client:
                response = await client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise TransientLinkedInError("Could not connect to LinkedIn") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if ambiguous_on_transport:
                raise AmbiguousLinkedInError("LinkedIn result is ambiguous") from exc
            raise TransientLinkedInError("LinkedIn transport error") from exc
        if response.status_code in (429, 500, 502, 503, 504):
            if ambiguous_on_transport and response.status_code != 429:
                raise AmbiguousLinkedInError(
                    f"LinkedIn returned an ambiguous {response.status_code} response"
                )
            raise TransientLinkedInError(f"LinkedIn temporary error {response.status_code}")
        if response.status_code >= 400:
            raise PermanentLinkedInError(
                response.status_code, f"LinkedIn rejected the request ({response.status_code})"
            )
        return response

    @retry(
        retry=retry_if_exception_type(TransientLinkedInError),
        stop=stop_after_attempt(5),
        wait=wait_random_exponential(min=2, max=60),
        reraise=True,
    )
    async def upload_image(self, image_content: bytes) -> str:
        init = await self._request(
            "POST",
            "/rest/images?action=initializeUpload",
            json={"initializeUploadRequest": {"owner": settings.linkedin_author_urn}},
        )
        data = init.json()["value"]
        upload_url = data["uploadUrl"]
        image_urn = data.get("image")
        if not isinstance(image_urn, str) or not re.fullmatch(
            r"urn:li:image:[A-Za-z0-9_-]{1,300}", image_urn
        ):
            raise PermanentLinkedInError(502, "LinkedIn returned an invalid image URN")
        parsed_upload = urlparse(upload_url)
        try:
            upload_port = parsed_upload.port
        except ValueError as exc:
            raise PermanentLinkedInError(502, "LinkedIn returned an invalid upload URL") from exc
        upload_host = (parsed_upload.hostname or "").casefold()
        trusted_upload_host = upload_host in {"linkedin.com", "licdn.com"} or upload_host.endswith(
            (".linkedin.com", ".licdn.com")
        )
        if (
            parsed_upload.scheme != "https"
            or not trusted_upload_host
            or parsed_upload.username
            or parsed_upload.password
            or upload_port not in {None, 443}
        ):
            raise PermanentLinkedInError(502, "LinkedIn returned an untrusted upload URL")
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                uploaded = await client.put(
                    upload_url,
                    content=image_content,
                    headers={"Authorization": self.headers["Authorization"]},
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientLinkedInError("LinkedIn image upload transport error") from exc
        if uploaded.status_code in (300, 301, 302, 303, 307, 308, 429, 500, 502, 503, 504):
            raise TransientLinkedInError(
                f"LinkedIn image upload temporary error {uploaded.status_code}"
            )
        if uploaded.status_code >= 400:
            raise PermanentLinkedInError(
                uploaded.status_code,
                f"LinkedIn rejected the image upload ({uploaded.status_code})",
            )
        return image_urn

    async def publish(self, text: str, image_content: bytes | None = None) -> tuple[str, str]:
        if settings.dry_run:
            return (
                "urn:li:share:dry-run",
                "https://www.linkedin.com/feed/update/urn:li:share:dry-run",
            )
        content = None
        if image_content:
            try:
                image_urn = await self.upload_image(image_content)
            except (TransientLinkedInError, PermanentLinkedInError):
                raise
            except Exception as exc:
                raise TransientLinkedInError("LinkedIn image preparation failed") from exc
            content = {"media": {"id": image_urn}}
        body = {
            "author": settings.linkedin_author_urn,
            "commentary": text,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if content:
            body["content"] = content
        response = await self._request(
            "POST",
            "/rest/posts",
            ambiguous_on_transport=True,
            json=body,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        urn = response.headers.get("x-restli-id")
        if not isinstance(urn, str) or not re.fullmatch(
            r"urn:li:(?:share|ugcPost):[A-Za-z0-9_-]{1,200}", urn
        ):
            raise AmbiguousLinkedInError("LinkedIn did not return a valid post receipt")
        return urn, f"https://www.linkedin.com/feed/update/{urn}"

    async def post_metric(self, post_urn: str, metric: str) -> tuple[int, dict]:
        urn_type = "share" if ":share:" in post_urn else "ugc"
        entity = f"({urn_type}:{post_urn})"
        response = await self._request(
            "GET",
            "/rest/memberCreatorPostAnalytics",
            params={
                "q": "entity",
                "entity": entity,
                "queryType": metric,
                "aggregation": "TOTAL",
            },
        )
        payload = response.json()
        count = sum(int(element.get("count", 0)) for element in payload.get("elements", []))
        return count, payload
