"""Tests for Drive-sourced Gmail attachments and the local-path guard.

``_resolve_drive_attachments`` lets a caller reference a Google Drive file by id
instead of a server-local path (which is meaningless when the server runs
remotely). The file is either downloaded and attached as binary, or — with
``as_link`` — surfaced as a share link appended to the body.
"""

import asyncio
from unittest.mock import Mock, patch

from gmail.gmail_tools import (
    _resolve_drive_attachments,
    _append_drive_links_to_body,
)


def _drive_service_mock(meta, *, media=b"", export=b""):
    svc = Mock()
    svc.files().get().execute.return_value = meta
    svc.files().get_media().execute.return_value = media
    svc.files().export().execute.return_value = export
    return svc


def test_drive_attachment_downloaded_as_binary():
    """A binary Drive file is downloaded via get_media and attached as bytes."""
    drive = _drive_service_mock(
        {"name": "report.pdf", "mimeType": "application/pdf"},
        media=b"%PDF-bytes",
    )
    with (
        patch("gmail.gmail_tools.build", return_value=drive),
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
    ):
        resolved, links = asyncio.run(
            _resolve_drive_attachments(Mock(), [{"drive_file_id": "f1"}])
        )

    assert links == []
    assert resolved[0]["_resolved_bytes"] == b"%PDF-bytes"
    assert resolved[0]["filename"] == "report.pdf"
    assert resolved[0]["mime_type"] == "application/pdf"


def test_native_drive_file_exported_to_pdf():
    """Native Docs/Sheets/Slides are exported to PDF (get_media can't fetch them)."""
    drive = _drive_service_mock(
        {"name": "Spec", "mimeType": "application/vnd.google-apps.document"},
        export=b"%PDF-export",
    )
    with (
        patch("gmail.gmail_tools.build", return_value=drive),
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
    ):
        resolved, links = asyncio.run(
            _resolve_drive_attachments(Mock(), [{"drive_file_id": "doc1"}])
        )

    assert resolved[0]["_resolved_bytes"] == b"%PDF-export"
    assert resolved[0]["filename"] == "Spec.pdf"
    assert resolved[0]["mime_type"] == "application/pdf"
    drive.files.return_value.export.assert_called()


def test_drive_attachment_as_link_goes_to_body():
    """as_link surfaces a share link instead of attaching bytes."""
    drive = _drive_service_mock(
        {
            "name": "Deck",
            "mimeType": "application/vnd.google-apps.presentation",
            "webViewLink": "https://drive.google.com/file/d/deck1/view",
        }
    )
    with (
        patch("gmail.gmail_tools.build", return_value=drive),
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
    ):
        resolved, links = asyncio.run(
            _resolve_drive_attachments(
                Mock(), [{"drive_file_id": "deck1", "as_link": True}]
            )
        )

    assert resolved == []  # nothing attached as bytes
    assert links == [
        {"name": "Deck", "url": "https://drive.google.com/file/d/deck1/view"}
    ]


def test_local_path_rejected_when_local_files_disabled():
    """A local 'path' attachment becomes an error entry when the server cannot
    read local files (WORKSPACE_MCP_DISABLE_LOCAL_FILES=true / stateless)."""
    with patch("gmail.gmail_tools.local_file_access_enabled", return_value=False):
        resolved, links = asyncio.run(
            _resolve_drive_attachments(Mock(), [{"path": "/tmp/secret.pdf"}])
        )

    assert links == []
    assert resolved[0].get("error")
    assert "WORKSPACE_MCP_DISABLE_LOCAL_FILES" in resolved[0]["error"]
    assert "drive_file_id" in resolved[0]["error"]


def test_local_path_allowed_when_local_files_enabled():
    """With local file access enabled a 'path' attachment passes through untouched."""
    with patch("gmail.gmail_tools.local_file_access_enabled", return_value=True):
        att = {"path": "/tmp/report.pdf"}
        resolved, links = asyncio.run(_resolve_drive_attachments(Mock(), [att]))

    assert links == []
    assert resolved == [att]


def test_local_path_guard_ignores_transport():
    """The guard follows the local-files setting, not the transport: a
    streamable-http server on localhost shares the caller's disk (the
    WORKSPACE_MCP_DISABLE_LOCAL_FILES rationale), so 'path' still works there."""
    att = {"path": "/tmp/report.pdf"}
    with (
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
        patch("gmail.gmail_tools.local_file_access_enabled", return_value=True),
    ):
        resolved, _ = asyncio.run(_resolve_drive_attachments(Mock(), [att]))
    assert resolved == [att]

    with (
        patch("gmail.gmail_tools.get_transport_mode", return_value="stdio"),
        patch("gmail.gmail_tools.local_file_access_enabled", return_value=False),
    ):
        resolved, _ = asyncio.run(_resolve_drive_attachments(Mock(), [att]))
    assert resolved[0].get("error")


def test_append_drive_links_plain_and_html():
    links = [{"name": "Deck", "url": "https://x/deck"}]
    plain = _append_drive_links_to_body("Hi", links, "plain")
    assert "https://x/deck" in plain and "Deck" in plain
    html = _append_drive_links_to_body("<p>Hi</p>", links, "html")
    assert '<a href="https://x/deck">Deck</a>' in html


# --- A Drive attachment this server cannot read refuses the whole message ---

import base64  # noqa: E402
import logging  # noqa: E402

import pytest  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402

from core.utils import UserInputError  # noqa: E402
from gmail.gmail_tools import draft_gmail_message, send_gmail_message  # noqa: E402


class _Resp(dict):
    def __init__(self, status):
        super().__init__()
        self.status = status
        self.reason = "x"


def _err(status, content=b"{}"):
    return HttpError(_Resp(status), content)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _unreadable_drive(status):
    drive = Mock()
    drive.files().get().execute.side_effect = _err(status)
    return drive


INLINE = {
    "content": base64.b64encode(b"hello").decode(),
    "filename": "note.txt",
}


def _gmail_service():
    service = Mock()
    service.users().messages().send().execute.return_value = {"id": "sent1"}
    service.users().drafts().create().execute.return_value = {"id": "d1"}
    service.users().settings().sendAs().list().execute.return_value = {"sendAs": []}
    service.users().messages().send.reset_mock()
    service.users().drafts().create.reset_mock()
    return service


@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize(
    "attachments",
    [[{"drive_file_id": "f1"}], [{"drive_file_id": "f1"}, INLINE]],
    ids=["drive-only", "drive-plus-inline"],
)
def test_unreadable_drive_attachment_refuses_the_send(status, attachments):
    """Nothing is sent: a partial send would put an incomplete message in
    someone's inbox, and a sent message cannot be recalled."""
    service = _gmail_service()
    with (
        patch("gmail.gmail_tools.build", return_value=_unreadable_drive(status)),
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
        pytest.raises(UserInputError) as exc,
    ):
        asyncio.run(
            _unwrap(send_gmail_message)(
                service=service,
                user_google_email="user@example.com",
                to="rcpt@example.com",
                subject="Hi",
                body="Hello",
                attachments=attachments,
                include_signature=False,
            )
        )
    text = str(exc.value)
    assert "f1" in text and f"HTTP {status}" in text
    assert "draft_id" in text and "nothing was sent" in text
    service.users.return_value.messages.return_value.send.assert_not_called()


def test_unreadable_drive_attachment_refuses_the_draft():
    service = _gmail_service()
    with (
        patch("gmail.gmail_tools.build", return_value=_unreadable_drive(404)),
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
        pytest.raises(UserInputError, match="could not be read"),
    ):
        asyncio.run(
            _unwrap(draft_gmail_message)(
                service=service,
                user_google_email="user@example.com",
                to="rcpt@example.com",
                subject="Hi",
                body="Hello",
                attachments=[{"drive_file_id": "f1"}, INLINE],
                include_signature=False,
            )
        )
    service.users.return_value.drafts.return_value.create.assert_not_called()


def test_unreadable_download_also_refuses():
    """The metadata read can succeed while the download is refused."""
    drive = Mock()
    drive.files().get().execute.return_value = {
        "name": "r.pdf",
        "mimeType": "application/pdf",
    }
    drive.files().get_media().execute.side_effect = _err(403)
    with (
        patch("gmail.gmail_tools.build", return_value=drive),
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
        pytest.raises(UserInputError, match="HTTP 403"),
    ):
        asyncio.run(_resolve_drive_attachments(Mock(), [{"drive_file_id": "f1"}]))


def test_server_error_keeps_the_per_attachment_handling():
    """Only permission-shaped failures abort; a 5xx keeps upstream's design."""
    with (
        patch("gmail.gmail_tools.build", return_value=_unreadable_drive(500)),
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
    ):
        resolved, _ = asyncio.run(
            _resolve_drive_attachments(Mock(), [{"drive_file_id": "f1"}])
        )
    assert len(resolved) == 1 and "_resolved_bytes" not in resolved[0]


def test_rate_limited_403_keeps_the_per_attachment_handling():
    drive = Mock()
    drive.files().get().execute.side_effect = _err(
        403, b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}'
    )
    with (
        patch("gmail.gmail_tools.build", return_value=drive),
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
    ):
        resolved, _ = asyncio.run(
            _resolve_drive_attachments(Mock(), [{"drive_file_id": "f1"}])
        )
    assert len(resolved) == 1 and "_resolved_bytes" not in resolved[0]


def test_unreadable_drive_attachment_logs_a_warning_without_traceback(caplog):
    with (
        patch("gmail.gmail_tools.build", return_value=_unreadable_drive(404)),
        patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http"),
        caplog.at_level(logging.WARNING, logger="gmail.gmail_tools"),
        pytest.raises(UserInputError),
    ):
        asyncio.run(_resolve_drive_attachments(Mock(), [{"drive_file_id": "f1"}]))
    records = [r for r in caplog.records if "f1" in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING for r in records)
    assert all(r.exc_info is None for r in records)
