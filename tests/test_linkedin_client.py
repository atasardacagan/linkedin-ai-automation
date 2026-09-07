from unittest.mock import AsyncMock, Mock

import pytest

from linkedin_automation.linkedin import (
    AmbiguousLinkedInError,
    LinkedInClient,
    PermanentLinkedInError,
)


@pytest.mark.asyncio
async def test_dry_run_never_calls_network(monkeypatch):
    from linkedin_automation import linkedin

    monkeypatch.setattr(linkedin.settings, "dry_run", True)
    urn, url = await LinkedInClient().publish("Approved copy")
    assert urn == "urn:li:share:dry-run"
    assert url.endswith("urn:li:share:dry-run")


@pytest.mark.asyncio
async def test_image_dry_run_never_uploads(monkeypatch):
    from linkedin_automation import linkedin

    monkeypatch.setattr(linkedin.settings, "dry_run", True)
    client = LinkedInClient()
    client.upload_image = AsyncMock(side_effect=AssertionError("must not upload"))
    await client.publish("Approved copy", "storage/example.png")
    client.upload_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_analytics_uses_restli_entity_tuple(monkeypatch):
    from linkedin_automation import linkedin

    monkeypatch.setattr(linkedin.settings, "dry_run", False)
    client = LinkedInClient()
    response = Mock()
    response.json.return_value = {"elements": [{"count": 7}]}
    client._request = AsyncMock(return_value=response)

    count, _ = await client.post_metric("urn:li:ugcPost:123", "REACTION")

    assert count == 7
    assert client._request.await_args.kwargs["params"]["entity"] == "(ugc:urn:li:ugcPost:123)"


@pytest.mark.asyncio
async def test_image_upload_rejects_untrusted_bearer_destination():
    client = LinkedInClient()
    response = Mock()
    response.json.return_value = {
        "value": {
            "uploadUrl": "https://attacker.example/upload",
            "image": "urn:li:image:example",
        }
    }
    client._request = AsyncMock(return_value=response)

    with pytest.raises(PermanentLinkedInError, match="untrusted upload URL"):
        await client.upload_image(b"png")


@pytest.mark.asyncio
async def test_image_upload_rejects_invalid_asset_receipt():
    client = LinkedInClient()
    response = Mock()
    response.json.return_value = {
        "value": {
            "uploadUrl": "https://www.linkedin.com/dms-uploads/example",
            "image": "not-a-linkedin-image-urn",
        }
    }
    client._request = AsyncMock(return_value=response)

    with pytest.raises(PermanentLinkedInError, match="invalid image URN"):
        await client.upload_image(b"png")


@pytest.mark.asyncio
async def test_publish_treats_missing_post_receipt_as_ambiguous(monkeypatch):
    from linkedin_automation import linkedin

    monkeypatch.setattr(linkedin.settings, "dry_run", False)
    client = LinkedInClient()
    response = Mock()
    response.headers = {}
    client._request = AsyncMock(return_value=response)

    with pytest.raises(AmbiguousLinkedInError, match="valid post receipt"):
        await client.publish("Approved copy")
