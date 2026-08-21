"""Tests for attaching one of this server's OWN signed download URLs.

`get_drive_file_download_url` mints `/dl/{handle}` (default) or
`/attachments/signed/{token}`. Neither is a usable attachment *source*:
`_try_read_local_attachment` only short-circuits the bare two-segment
`/attachments/{id}` plane, so a signed URL falls through to an HTTP fetch of
the server by itself, which the SSRF guard blocks on any deploy whose external
base URI is localhost or a private address.

That produced a four-attempt failure loop in the field (localhost rejected ->
host.docker.internal rejected -> `path` rejected -> `drive_file_id` worked).
These tests pin the two halves of the fix: the route is recognised, and the
error routes the caller to something that actually works.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from gmail.gmail_tools import _is_own_download_route, _resolve_url_attachments


class TestIsOwnDownloadRoute:
    def test_short_claim_check_route(self):
        assert _is_own_download_route("http://localhost:8000/dl/AbCdEf123456")

    def test_signed_token_route(self):
        assert _is_own_download_route(
            "https://mcp.example.com/attachments/signed/eyJhbGciOiJIUzI1NiJ9.x.y"
        )

    def test_bare_attachments_plane_is_not_this_case(self):
        """`/attachments/{id}` IS resolvable locally, so it must not be caught."""
        assert not _is_own_download_route("http://localhost:8000/attachments/abc-123")

    def test_genuinely_external_urls(self):
        assert not _is_own_download_route("https://example.com/files/report.pdf")
        assert not _is_own_download_route("https://example.com/dl/a/b/c")

    def test_path_only_urls(self):
        """The minted form may be relative depending on the base URI."""
        assert _is_own_download_route("/dl/AbCdEf123456")
        assert not _is_own_download_route("/dl")


@pytest.mark.asyncio
async def test_own_download_url_error_names_the_working_alternatives(monkeypatch):
    """The SSRF rejection must be replaced with routing guidance."""

    async def _boom(url):
        raise ValueError("URLs pointing to localhost are not allowed")

    monkeypatch.setattr("gmail.gmail_tools._download_attachment_bytes", _boom)

    resolved = await _resolve_url_attachments(
        [{"url": "http://localhost:8000/dl/AbCdEf123456", "filename": "report.pdf"}]
    )

    error = resolved[0]["error"]
    assert "server's own download link" in error
    assert "drive_file_id" in error
    assert "return_base64=True" in error
    # The bare SSRF text invited the retry-with-another-hostname loop.
    assert "localhost are not allowed" not in error


@pytest.mark.asyncio
async def test_external_url_failure_keeps_its_own_error(monkeypatch):
    """Guidance must not be pasted over unrelated fetch failures."""

    async def _boom(url):
        raise ValueError("connection refused")

    monkeypatch.setattr("gmail.gmail_tools._download_attachment_bytes", _boom)

    resolved = await _resolve_url_attachments(
        [{"url": "https://example.com/report.pdf", "filename": "report.pdf"}]
    )

    error = resolved[0]["error"]
    assert "connection refused" in error
    assert "drive_file_id" not in error
