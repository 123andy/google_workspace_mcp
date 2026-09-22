"""Gmail tools hand out signed download URLs instead of fetching the bytes, and say
so loudly when they cannot."""

import base64
from unittest.mock import Mock, patch

import pytest

import core.signed_downloads as sd
from gmail.gmail_tools import _export_full_message, get_gmail_attachment_content

USER = "user@example.com"
URL = "https://mcp.example.com/attachments/signed/TOKEN"
HEADERS = {"Subject": "Quarterly numbers", "From": "cfo@example.com"}


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _service(payload=b"bytes", filename="report.pdf"):
    service = Mock()
    service.users().messages().attachments().get().execute.return_value = {
        "size": len(payload),
        "data": base64.urlsafe_b64encode(payload).decode(),
    }
    service.users().messages().get().execute.return_value = {
        "payload": {
            "parts": [
                {
                    "filename": filename,
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": "att-1", "size": len(payload)},
                }
            ]
        }
    }
    return service


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", "true")
    monkeypatch.setattr(sd, "get_transport_mode", lambda: "streamable-http")
    monkeypatch.delenv("WORKSPACE_MCP_MAX_FILE_BYTES", raising=False)


@pytest.mark.asyncio
async def test_attachment_returns_signed_url_without_downloading(enabled):
    service = _service()
    with patch.object(sd, "offer_url", return_value=(URL, 540)) as offer:
        result = await _unwrap(get_gmail_attachment_content)(
            service=service,
            message_id="msg-1",
            attachment_id="att-1",
            user_google_email=USER,
        )

    assert URL in result and "~9 minutes" in result
    assert "Filename: report.pdf" in result
    service.users().messages().attachments().get().execute.assert_not_called()
    assert offer.call_args.kwargs["source"] == "gmail"
    assert offer.call_args.kwargs["ref"] == {"mid": "msg-1", "aid": "att-1"}
    assert offer.call_args.kwargs["filename"] == "report.pdf"
    assert offer.call_args.args == (USER,)


@pytest.mark.asyncio
async def test_return_base64_bypasses_signed_url(enabled, monkeypatch):
    """Callers who ask for inline bytes cannot reach a URL; give them the bytes."""
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: True)
    with patch.object(sd, "offer_url") as offer:
        result = await _unwrap(get_gmail_attachment_content)(
            service=_service(b"hello"),
            message_id="msg-1",
            attachment_id="att-1",
            user_google_email=USER,
            return_base64=True,
        )
    offer.assert_not_called()
    assert base64.b64encode(b"hello").decode() in result
    assert "NO download URL" not in result


@pytest.mark.asyncio
async def test_stateless_fallback_is_loud_when_url_cannot_be_minted(
    enabled, monkeypatch
):
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: True)
    with patch.object(sd, "offer_url", return_value=None):
        result = await _unwrap(get_gmail_attachment_content)(
            service=_service(),
            message_id="msg-1",
            attachment_id="att-1",
            user_google_email=USER,
        )
    assert "downloaded successfully" not in result
    assert "NO download URL could be issued" in result
    assert "could not recover usable credentials" in result


@pytest.mark.asyncio
async def test_stateless_wording_unchanged_when_feature_off(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_MAX_FILE_BYTES", raising=False)
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: True)
    result = await _unwrap(get_gmail_attachment_content)(
        service=_service(),
        message_id="msg-1",
        attachment_id="att-1",
        user_google_email=USER,
    )
    assert result.startswith("Attachment downloaded successfully!")
    assert "signed" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_format, extension",
    [("raw", ".eml"), ("html", ".html"), ("text", ".txt")],
)
async def test_full_export_offers_signed_url_and_skips_fetch(
    enabled, body_format, extension
):
    service = Mock()
    with patch.object(sd, "offer_url", return_value=(URL, 900)) as offer:
        result = await _export_full_message(
            service, "msg-1", HEADERS, body_format, user_google_email=USER
        )

    assert "FULL MESSAGE EXPORT (download link)" in result and URL in result
    assert "Content is NOT included" in result
    service.users.assert_not_called()  # the route fetches at download time
    kwargs = offer.call_args.kwargs
    assert kwargs["source"] == "gmail_message"
    assert kwargs["ref"] == {"mid": "msg-1", "fmt": body_format}
    assert kwargs["filename"] == f"Quarterly numbers{extension}"


@pytest.mark.asyncio
async def test_full_export_falls_back_to_upstream_path(enabled, monkeypatch):
    """No URL: upstream's stateless inline delivery, complete and untruncated."""
    import gmail.gmail_tools as gmail_tools

    monkeypatch.setattr(gmail_tools, "is_stateless_mode", lambda: True)
    service = Mock()
    service.users().messages().get().execute.return_value = {"raw": "SGVsbG8gd29ybGQ="}

    with patch.object(sd, "offer_url", return_value=None):
        result = await _export_full_message(
            service, "msg-3", HEADERS, "raw", user_google_email=USER
        )

    assert "Hello world" in result
    assert "signed URL" not in result


class _Saved:
    path, file_id = "/nonexistent/Quarterly numbers.eml", "file-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [True, False])
async def test_full_export_fallback_is_loud_when_url_cannot_be_minted(
    enabled, monkeypatch, stateless
):
    """Same contract as the attachment and Drive tools: signed links on, offer
    refused, so the fallback (inline or stored) says no signed URL was issued."""
    import gmail.gmail_tools as gmail_tools

    monkeypatch.setattr(gmail_tools, "is_stateless_mode", lambda: stateless)
    monkeypatch.setattr(gmail_tools, "get_transport_mode", lambda: "streamable-http")
    storage = Mock()
    storage.save_attachment_bytes.return_value = _Saved()
    monkeypatch.setattr(gmail_tools, "get_attachment_storage", lambda: storage)
    monkeypatch.setattr(gmail_tools, "get_attachment_url", lambda fid: f"/a/{fid}")
    service = Mock()
    service.users().messages().get().execute.return_value = {"raw": "SGVsbG8gd29ybGQ="}

    with patch.object(sd, "offer_url", return_value=None):
        result = await _export_full_message(
            service, "msg-4", HEADERS, "raw", user_google_email=USER
        )

    assert "Error" not in result
    assert sd.UNAVAILABLE_NOTE in result


@pytest.mark.asyncio
async def test_full_export_fallback_has_no_note_when_feature_off(monkeypatch):
    import gmail.gmail_tools as gmail_tools

    monkeypatch.delenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_MAX_FILE_BYTES", raising=False)
    monkeypatch.setattr(gmail_tools, "is_stateless_mode", lambda: True)
    service = Mock()
    service.users().messages().get().execute.return_value = {"raw": "SGVsbG8gd29ybGQ="}

    result = await _export_full_message(
        service, "msg-5", HEADERS, "raw", user_google_email=USER
    )

    assert "Hello world" in result
    assert sd.UNAVAILABLE_NOTE not in result
