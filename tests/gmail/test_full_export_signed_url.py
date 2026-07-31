"""
Fork graft tests: signed-URL delivery for the full message export.

Upstream delivers get_gmail_message_content(full=True) via _export_full_message
(disk + URL/path, or inline in stateless mode). The fork grafts a signed-URL
branch at the top: when WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS is enabled and the
user's credentials are recoverable, the export returns a short-lived signed URL
(the "gmail_message" fetcher in core.signed_download streams the message from
Gmail at download time) and never fetches, writes, or inlines the body.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gmail.gmail_tools import _export_full_message

HEADERS = {"Subject": "Quarterly numbers", "From": "cfo@example.com"}


def _future_creds():
    creds = Mock()
    creds.expiry = datetime.utcnow() + timedelta(hours=1)
    return creds


def _signed_patches(store_behavior):
    """Patch the signed-delivery collaborators; store_behavior configures
    get_credentials (a Mock side_effect/return_value)."""
    store = Mock()
    if isinstance(store_behavior, Exception):
        store.get_credentials.side_effect = store_behavior
    else:
        store.get_credentials.return_value = store_behavior
    return (
        patch("core.attachment_signing.signed_attachment_urls_enabled", lambda: True),
        patch("core.attachment_signing.clamp_ttl_to_expiry", lambda expiry: 3600),
        patch(
            "core.attachment_signing.build_download_url",
            AsyncMock(return_value="https://mcp.example.com/dl/abc123"),
        ),
        patch("auth.oauth21_session_store.get_oauth21_session_store", lambda: store),
        patch("core.attachment_cred_cache.stash_credentials", AsyncMock()),
    )


@pytest.mark.asyncio
async def test_signed_url_delivery_skips_fetch_and_inline():
    service = Mock()
    patches = _signed_patches(_future_creds())
    with patches[0], patches[1], patches[2] as mock_build, patches[3], patches[4]:
        result = await _export_full_message(
            service, "msg-1", HEADERS, "raw", "user@example.com"
        )

    assert "FULL MESSAGE EXPORT (signed URL)" in result
    assert "https://mcp.example.com/dl/abc123" in result
    assert "Content is NOT included" in result
    # The signed path must not touch Gmail at export time — the fetcher does that
    # at download time.
    service.users.assert_not_called()
    kwargs = mock_build.call_args.kwargs
    assert kwargs["source"] == "gmail_message"
    assert kwargs["ref"] == {"mid": "msg-1", "fmt": "eml"}
    assert kwargs["mime_type"] == "message/rfc822"
    assert kwargs["filename"].endswith(".eml")


@pytest.mark.asyncio
async def test_body_format_maps_to_fetcher_fmt():
    service = Mock()
    patches = _signed_patches(_future_creds())
    with patches[0], patches[1], patches[2] as mock_build, patches[3], patches[4]:
        await _export_full_message(
            service, "msg-2", HEADERS, "text", "user@example.com"
        )
    kwargs = mock_build.call_args.kwargs
    assert kwargs["ref"] == {"mid": "msg-2", "fmt": "txt"}
    assert kwargs["mime_type"] == "text/plain"


@pytest.mark.asyncio
async def test_falls_back_to_upstream_path_without_credentials(monkeypatch):
    """No recoverable credentials → the graft steps aside; upstream's stateless
    inline branch (complete, untruncated) handles delivery."""
    import gmail.gmail_tools as gmail_tools

    service = Mock()
    raw = Mock()
    raw.execute.return_value = {"raw": "SGVsbG8gd29ybGQ="}  # "Hello world"
    service.users.return_value.messages.return_value.get.return_value = raw

    monkeypatch.setattr(gmail_tools, "is_stateless_mode", lambda: True)
    patches = _signed_patches(RuntimeError("no session"))
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await _export_full_message(
            service, "msg-3", HEADERS, "raw", "user@example.com"
        )

    assert "Hello world" in result  # inlined, complete
    assert "dl/abc123" not in result
