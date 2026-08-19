"""get_drive_file_download_url must be honest when it cannot issue a URL.

The stateless preview branch (no file storage + signed URL unavailable) used
to open with "File downloaded successfully!" and a bare truncated preview —
audit finding F5. It must name the cause and the remedy instead.
"""

import os
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from gdrive.drive_tools import get_drive_file_download_url  # noqa: E402


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.mark.asyncio
@patch("gdrive.drive_tools._download_file_bytes", new_callable=AsyncMock)
async def test_stateless_no_url_fallback_names_cause_and_remedy(
    mock_download, monkeypatch
):
    import core.attachment_signing as signing_module
    import gdrive.drive_tools as drive_tools_module

    # drive_tools binds is_stateless_mode at import time — patch ITS binding.
    monkeypatch.setattr(drive_tools_module, "is_stateless_mode", lambda: True)
    monkeypatch.setattr(
        signing_module, "signed_attachment_urls_enabled", lambda: False
    )
    mock_download.return_value = b"pdf bytes " + bytes(range(200))
    service = Mock()
    service.files().get().execute.return_value = {
        "name": "report.pdf",
        "mimeType": "application/pdf",
    }

    result = await _unwrap(get_drive_file_download_url)(
        service=service,
        user_google_email="user@example.com",
        file_id="f-1",
    )

    assert "downloaded successfully" not in result
    assert "NO download URL" in result
    assert "signed URL" in result and "stateless" in result
    assert "get_drive_file_content" in result
    assert "PREVIEW ONLY" in result
