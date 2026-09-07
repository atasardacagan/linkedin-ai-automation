import asyncio
import ipaddress
import re
import socket
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urljoin, urlparse, urlunparse

import httpx


def _public_addresses(hostname: str) -> list[str]:
    try:
        addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    public_addresses = []
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            return []
        normalized = str(ip)
        if normalized not in public_addresses:
            public_addresses.append(normalized)
    return public_addresses


def _pinned_request(url: str, ip: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")
    hostname = hostname.encode("idna").decode("ascii")
    port = parsed.port
    ip_literal = f"[{ip}]" if ":" in ip else ip
    pinned_netloc = f"{ip_literal}:{port}" if port else ip_literal
    pinned_url = urlunparse(parsed._replace(netloc=pinned_netloc))
    default_port = 443 if parsed.scheme == "https" else 80
    host_header = f"{hostname}:{port}" if port and port != default_port else hostname
    return pinned_url, host_header, hostname


async def validate_source(source: dict) -> dict | None:
    url = str(source.get("url", ""))
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
    ):
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
            for _ in range(5):
                parsed = urlparse(url)
                try:
                    port = parsed.port
                except ValueError:
                    return None
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or port not in {None, 80, 443}
                ):
                    return None
                addresses = await asyncio.to_thread(_public_addresses, parsed.hostname)
                if not addresses:
                    return None
                pinned_url, host_header, sni_hostname = _pinned_request(url, addresses[0])
                async with client.stream(
                    "GET",
                    pinned_url,
                    headers={
                        "Host": host_header,
                        "User-Agent": "LinkedInContentResearch/1.0",
                    },
                    extensions={"sni_hostname": sni_hostname},
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        url = urljoin(url, location)
                        continue
                    if response.status_code >= 400:
                        return None
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" not in content_type and "text/plain" not in content_type:
                        return None
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 1_500_000:
                            return None
                        chunks.append(chunk)
                    body = b"".join(chunks).decode(response.encoding or "utf-8", errors="ignore")
                    break
            else:
                return None
    except httpx.HTTPError:
        return None
    page_text = unescape(re.sub(r"<[^>]+>", " ", body))
    page_words = set(re.findall(r"[\w%-]{4,}", page_text.casefold()))
    fact_words = set(re.findall(r"[\w%-]{4,}", str(source.get("fact", "")).casefold()))
    if fact_words and len(fact_words & page_words) / len(fact_words) < 0.55:
        return None
    validated = dict(source)
    validated["resolved_url"] = url
    validated["checked_at"] = datetime.now(UTC).isoformat()
    validated["supporting_term_overlap"] = (
        round(len(fact_words & page_words) / len(fact_words), 3) if fact_words else 0
    )
    return validated


async def validate_sources(sources: list[dict]) -> list[dict]:
    results = await asyncio.gather(*(validate_source(source) for source in sources[:8]))
    return [source for source in results if source is not None]
