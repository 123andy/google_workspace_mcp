"""The ``chat`` signed-download source: Chat attachment bytes streamed from the
media endpoint, with the resource-name guards and size cap."""

import httpx
import pytest

import core.signed_downloads as sd

RN = "spaces/S/attachments/A"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        None,
        "spaces/../x",
        "spaces/S?x=1",
        "spaces/S#frag",
        "spaces/S%2e%2e/x",
        "spaces\\S",
        "spaces/S\nattachments",
        "spaces/S\x00",
        "spaces/S\x7f",
    ],
)
def test_resource_name_guards(bad):
    with pytest.raises(ValueError):
        sd.chat_media_url(bad)


def test_url_is_pinned_to_the_chat_host():
    assert sd.chat_media_url(RN) == (
        "https://chat.googleapis.com/v1/media/spaces/S/attachments/A?alt=media"
    )
    # A name that looks like an authority or a scheme stays a path suffix.
    for name in ("@evil.example/x", "//evil.example/x", "https://evil.example/x"):
        assert httpx.URL(sd.chat_media_url(name)).host == "chat.googleapis.com"


def test_name_is_percent_encoded_as_a_path():
    url = sd.chat_media_url("spaces/S/attachments/a b")
    assert "a%20b" in url


@pytest.mark.asyncio
async def test_missing_rn_claim_is_a_fetch_error():
    with pytest.raises(sd.SignedDownloadError):
        await sd._fetch_chat({}, object())
