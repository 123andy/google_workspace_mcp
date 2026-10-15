"""With the signed-link settings off, tool output and routes are exactly what they
were before signed uploads (and Chat signed links) existed.

The expected strings are literals on purpose, and the file imports nothing
added by that work, so the same file run against the pre-feature tree must pass
too: that is the byte-for-byte check. The upload-link setting is also set to
"true" under stdio, where it is ignored, to show it changes nothing there."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from core.config import get_transport_mode
from core.server import server, set_transport_mode

UPLOAD_URL = (
    "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=X"
)
USER = "user@example.com"


def _unwrap(tool):
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _service():
    response = Mock()
    response.status = 200
    response.get = Mock(return_value=UPLOAD_URL)
    service = Mock()
    service._http.request.return_value = (response, b"")
    return service


def _tail(mime):
    return (
        "Upload URL (no Authorization header required):\n"
        f"{UPLOAD_URL}\n\n"
        "Send the bytes with a single PUT, e.g.:\n"
        f"  curl -H 'Content-Type: {mime}' --upload-file \"/path/to/file\" "
        f"'{UPLOAD_URL}'\n\n"
        "The session takes one upload; an interrupted PUT can be resumed with "
        "Content-Range until Google expires the session (about a week). The file "
        "is created or updated only when the upload completes, and the final "
        "PUT's response body is the file's metadata (including its id)."
    )


@pytest.fixture(
    params=[
        ("off", "streamable-http"),
        ("unset", "streamable-http"),
        ("true", "stdio"),
    ],
    ids=["false-http", "unset-http", "true-stdio"],
)
def upload_setting(request, monkeypatch):
    value, transport = request.param
    if value == "unset":
        monkeypatch.delenv("WORKSPACE_MCP_SIGNED_UPLOAD_URLS", raising=False)
    else:
        monkeypatch.setenv(
            "WORKSPACE_MCP_SIGNED_UPLOAD_URLS", "false" if value == "off" else value
        )
    monkeypatch.setenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", "true")
    saved = get_transport_mode()
    set_transport_mode(transport)
    yield
    set_transport_mode(saved)


@pytest.fixture
def folder():
    resolve = AsyncMock(return_value="folder123")
    with (
        patch("gdrive.drive_tools.resolve_folder_id", resolve),
        patch("gdrive.drive_helpers.resolve_folder_id", resolve),
    ):
        yield resolve


@pytest.mark.asyncio
async def test_create_drive_file_upload_url_is_unchanged(upload_setting, folder):
    from gdrive.drive_tools import create_drive_file

    result = await _unwrap(create_drive_file)(
        service=_service(),
        user_google_email=USER,
        file_name="report.pdf",
        folder_id="target",
        mime_type="application/pdf",
        return_upload_url=True,
    )
    assert result == (
        "Resumable upload session created for new file 'report.pdf' "
        "(folder 'target', for user@example.com).\n\n" + _tail("application/pdf")
    )


@pytest.mark.asyncio
async def test_update_drive_file_upload_url_is_unchanged(upload_setting):
    from gdrive.drive_tools import update_drive_file

    with patch(
        "gdrive.drive_tools.resolve_drive_item",
        AsyncMock(
            return_value=("file123", {"name": "Notes", "mimeType": "text/plain"})
        ),
    ):
        result = await _unwrap(update_drive_file)(
            service=_service(),
            user_google_email=USER,
            file_id="file123",
            mime_type="text/plain",
            return_upload_url=True,
        )
    assert result == (
        "Resumable upload session created to replace the content of 'Notes' "
        "(ID file123, for user@example.com).\n\n" + _tail("text/plain")
    )


@pytest.mark.asyncio
async def test_import_upload_url_is_unchanged(upload_setting, folder):
    from gdrive.drive_tools import import_to_google_doc

    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    result = await _unwrap(import_to_google_doc)(
        service=_service(),
        user_google_email=USER,
        file_name="Report",
        source_format="docx",
        return_upload_url=True,
    )
    assert result == (
        "Resumable upload session created to import 'Report' as Google Doc "
        f"({docx} → application/vnd.google-apps.document, folder 'root', "
        "for user@example.com).\n\n" + _tail(docx)
    )


@pytest.mark.asyncio
async def test_ul_path_does_not_exist(upload_setting):
    import core.server as core_server

    # Newer trees mount /ul/ from configure_server_for_http; run that step too.
    mount = getattr(core_server, "_ensure_upload_relay_route", None)
    before = list(server._additional_http_routes)
    try:
        if mount is not None:
            core_server._upload_relay_route_registered = False
            mount()
        assert server._additional_http_routes == before
        transport = httpx.ASGITransport(app=server.http_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            ul = await http.put("/ul/" + "A" * 22, content=b"x")
            other = await http.put("/nothing-here/" + "A" * 22, content=b"x")
    finally:
        server._additional_http_routes[:] = before
        if mount is not None:
            core_server._upload_relay_route_registered = False
    assert ul.status_code == 404
    assert (ul.status_code, ul.content) == (other.status_code, other.content)


# --- Chat, signing off -------------------------------------------------------------


def _chat_service(attachment):
    service = Mock()
    service.spaces().messages().get().execute.return_value = {
        "name": "spaces/S/messages/M",
        "attachment": [attachment],
    }
    service._http.credentials.token = "tok"
    return service


ATTACHMENT = {
    "name": "spaces/S/messages/M/attachments/A",
    "contentName": "image.png",
    "contentType": "image/png",
    "source": "UPLOADED_CONTENT",
    "attachmentDataRef": {"resourceName": "spaces/S/attachments/A"},
}


@pytest.fixture
def signing_off(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", raising=False)
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_UPLOAD_URLS", "true")
    saved = get_transport_mode()
    set_transport_mode("streamable-http")
    yield
    set_transport_mode(saved)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attachment",
    [
        ATTACHMENT,
        {
            **{k: v for k, v in ATTACHMENT.items() if k != "attachmentDataRef"},
            "source": "DRIVE_FILE",
            "driveDataRef": {"driveFileId": "1AbC"},
        },
    ],
    ids=["uploaded", "drive-file"],
)
async def test_chat_download_output_is_unchanged(signing_off, attachment):
    from gchat.chat_tools import download_chat_attachment

    saved = Mock(path="/tmp/image_x.png", file_id="x1")
    with (
        patch(
            "gchat.chat_tools.download_http_url_bytes",
            AsyncMock(return_value=b"0123456789"),
        ),
        patch("auth.oauth_config.is_stateless_mode", return_value=False),
        patch("core.attachment_storage.get_attachment_storage") as storage,
        patch(
            "core.attachment_storage.get_attachment_url",
            return_value="http://localhost:8000/attachments/x1",
        ),
    ):
        storage.return_value.save_attachment_bytes.return_value = saved
        result = await _unwrap(download_chat_attachment)(
            service=_chat_service(attachment),
            user_google_email=USER,
            message_id="spaces/S/messages/M",
            attachment_index=0,
        )
    assert result == (
        "Attachment downloaded: image.png\n"
        "Type: image/png\n"
        "Size: 0.0 KB (10 bytes)\n"
        "\nDownload URL: http://localhost:8000/attachments/x1\n"
        "\nThe file will expire after 1 hour."
    )


@pytest.mark.asyncio
async def test_chat_message_listing_is_unchanged(signing_off):
    import gchat.chat_tools as ct

    drive = {
        "name": "spaces/S/messages/M/attachments/A",
        "contentName": "plan.gdoc",
        "contentType": "application/vnd.google-apps.document",
        "source": "DRIVE_FILE",
        "driveDataRef": {"driveFileId": "1AbC"},
    }
    chat_service = Mock()
    chat_service.spaces().get().execute.return_value = {"displayName": "Team"}
    chat_service.spaces().messages().list().execute.return_value = {
        "messages": [
            {
                "name": "spaces/S/messages/M",
                "text": "see attached",
                "createTime": "2025-01-01T00:00:00Z",
                "sender": {"name": "users/1", "displayName": "U"},
                "attachment": [ATTACHMENT, drive],
            }
        ]
    }
    with patch("gchat.chat_tools._resolve_sender", AsyncMock(return_value="U")):
        result = await _unwrap(ct.get_messages)(
            chat_service=chat_service,
            people_service=Mock(),
            user_google_email=USER,
            space_id="spaces/S",
        )
    assert result == (
        "Messages from 'Team' (ID: spaces/S):\n\n"
        "[2025-01-01T00:00:00Z] U:\n"
        "  see attached\n"
        "  [attachment 0: image.png (image/png)]\n"
        "  Use download_chat_attachment(message_id='spaces/S/messages/M', "
        "attachment_index=0) to download\n"
        "  [attachment 1: plan.gdoc (application/vnd.google-apps.document)]\n"
        "  Use download_chat_attachment(message_id='spaces/S/messages/M', "
        "attachment_index=1) to download\n"
        "  (Message ID: spaces/S/messages/M)\n"
    )
