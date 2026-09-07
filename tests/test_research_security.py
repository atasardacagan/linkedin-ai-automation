import socket

import pytest

from linkedin_automation import research


def _address(family: socket.AddressFamily, value: str):
    if family == socket.AF_INET6:
        return family, socket.SOCK_STREAM, 6, "", (value, 0, 0, 0)
    return family, socket.SOCK_STREAM, 6, "", (value, 0)


def test_public_addresses_returns_unique_global_addresses(monkeypatch):
    monkeypatch.setattr(
        research.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            _address(socket.AF_INET, "8.8.8.8"),
            _address(socket.AF_INET, "8.8.8.8"),
            _address(socket.AF_INET6, "2606:4700:4700::1111"),
        ],
    )

    assert research._public_addresses("example.com") == [
        "8.8.8.8",
        "2606:4700:4700::1111",
    ]


def test_public_addresses_fails_closed_if_dns_contains_any_non_public_address(monkeypatch):
    monkeypatch.setattr(
        research.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            _address(socket.AF_INET, "8.8.8.8"),
            _address(socket.AF_INET, "127.0.0.1"),
        ],
    )

    assert research._public_addresses("rebinding.example") == []


def test_public_addresses_fails_closed_on_dns_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr(research.socket, "getaddrinfo", fail)

    assert research._public_addresses("missing.example") == []


def test_pinned_request_uses_ip_for_transport_and_hostname_for_host_and_sni():
    pinned_url, host_header, sni_hostname = research._pinned_request(
        "https://b\u00fccher.de/article?q=1", "2606:4700:4700::1111"
    )

    assert pinned_url == "https://[2606:4700:4700::1111]/article?q=1"
    assert host_header == "xn--bcher-kva.de"
    assert sni_hostname == "xn--bcher-kva.de"


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes = b""):
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.encoding = "utf-8"
        self.is_redirect = 300 <= status_code < 400

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_bytes(self):
        yield self.body


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]):
        self.responses = responses
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_validate_source_re_resolves_redirect_and_pins_each_request(monkeypatch):
    client = _FakeClient(
        [
            _FakeResponse(302, {"location": "https://redirect.example/article"}),
            _FakeResponse(
                200,
                {"content-type": "text/html; charset=utf-8"},
                b"<main>The supported product claim is documented here.</main>",
            ),
        ]
    )
    addresses = {
        "source.example": ["8.8.8.8"],
        "redirect.example": ["2606:4700:4700::1111"],
    }

    async def fake_to_thread(_function, hostname):
        return addresses[hostname]

    monkeypatch.setattr(research.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(research.asyncio, "to_thread", fake_to_thread)

    result = await research.validate_source(
        {
            "url": "https://source.example/start",
            "title": "Source",
            "fact": "supported product claim",
        }
    )

    assert result is not None
    assert result["resolved_url"] == "https://redirect.example/article"
    assert [request["url"] for request in client.requests] == [
        "https://8.8.8.8/start",
        "https://[2606:4700:4700::1111]/article",
    ]
    assert client.requests[0]["headers"]["Host"] == "source.example"
    assert client.requests[0]["extensions"] == {"sni_hostname": "source.example"}
    assert client.requests[1]["headers"]["Host"] == "redirect.example"
    assert client.requests[1]["extensions"] == {"sni_hostname": "redirect.example"}


@pytest.mark.asyncio
async def test_validate_source_rejects_redirect_to_non_public_host_before_request(monkeypatch):
    client = _FakeClient([_FakeResponse(302, {"location": "http://127.0.0.1/secret"})])

    async def fake_to_thread(_function, hostname):
        return ["8.8.8.8"] if hostname == "source.example" else []

    monkeypatch.setattr(research.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(research.asyncio, "to_thread", fake_to_thread)

    result = await research.validate_source(
        {"url": "https://source.example/start", "fact": "supported product claim"}
    )

    assert result is None
    assert len(client.requests) == 1
