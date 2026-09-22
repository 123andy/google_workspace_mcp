"""return_upload_url: bytes go client -> Google, never through this server.

Every tool that accepts file content can instead open a Google Drive resumable
upload session and hand back its pre-authorized URL. The properties pinned here:
the session is the only thing created server-side; the flag never rides along
with another content source (nothing is silently ignored); on update it only
means whole-file replacement and never mislabels a native Google file's bytes.
"""

import json
from unittest.mock import AsyncMock, Mock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from core.utils import handle_http_errors
from gdrive.drive_tools import (
    _initiate_resumable_upload_session,
    create_drive_file,
    import_to_google_doc,
    import_to_google_sheets,
    import_to_google_slides,
    update_drive_file,
)

UPLOAD_URL = (
    "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=X"
)
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC = "application/vnd.google-apps.document"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _session_response(status=200, location=UPLOAD_URL):
    """httplib2-style (response, content) pair returned by service._http.request."""
    response = Mock()
    response.status = status
    response.get = Mock(return_value=location)
    return response, b""


def _service(status=200, location=UPLOAD_URL):
    service = Mock()
    service._http.request.return_value = _session_response(status, location)
    return service


@pytest.fixture
def folder():
    with patch(
        "gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock
    ) as resolve:
        resolve.return_value = "folder123"
        yield resolve


@pytest.mark.asyncio
async def test_create_opens_post_session_and_creates_nothing_else(folder):
    service = _service()

    result = await _unwrap(create_drive_file)(
        service=service,
        user_google_email="user@example.com",
        file_name="report.pdf",
        folder_id="target",
        mime_type="application/pdf",
        return_upload_url=True,
    )

    (url,), kwargs = service._http.request.call_args
    assert url.startswith("https://www.googleapis.com/upload/drive/v3/files?")
    assert "uploadType=resumable" in url and "supportsAllDrives=true" in url
    assert kwargs["method"] == "POST"
    assert kwargs["headers"]["X-Upload-Content-Type"] == "application/pdf"
    assert json.loads(kwargs["body"]) == {
        "name": "report.pdf",
        "parents": ["folder123"],
        "mimeType": "application/pdf",
    }
    assert UPLOAD_URL in result
    assert "single-use" not in result
    assert "only when the upload completes" in result
    service.files.assert_not_called()  # no files().create — Google creates on PUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"content": "hi"},
        {"fileUrl": "https://example.com/x"},
        {"base64_content": "aGk=", "content_mime_type": "text/plain"},
    ],
    ids=["content", "fileUrl", "base64_content"],
)
async def test_create_rejects_every_other_source(extra):
    service = _service()
    with pytest.raises(ValueError, match="do not also pass"):
        await _unwrap(create_drive_file)(
            service=service,
            user_google_email="user@example.com",
            file_name="report.pdf",
            return_upload_url=True,
            **extra,
        )
    service._http.request.assert_not_called()


@pytest.mark.asyncio
async def test_create_rejects_folders():
    with pytest.raises(ValueError, match="not applicable to folders"):
        await _unwrap(create_drive_file)(
            service=_service(),
            user_google_email="user@example.com",
            file_name="Folder",
            mime_type="application/vnd.google-apps.folder",
            return_upload_url=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime_type",
    [GOOGLE_DOC, "application/vnd.google-apps.shortcut", "", "   "],
    ids=["native-doc", "shortcut", "empty", "whitespace"],
)
async def test_create_rejects_native_or_empty_mime_type(folder, mime_type):
    """A native type is a conversion target and can never describe the PUT's bytes;
    the import_to_google_* tools are the route, exactly as on the inline path."""
    service = _service()
    with pytest.raises(ValueError, match="import_to_google_doc"):
        await _unwrap(create_drive_file)(
            service=service,
            user_google_email="user@example.com",
            file_name="Report",
            mime_type=mime_type,
            return_upload_url=True,
        )
    service._http.request.assert_not_called()


@pytest.mark.asyncio
@patch("gdrive.drive_tools.resolve_drive_item", new_callable=AsyncMock)
async def test_update_opens_patch_session_then_applies_metadata(resolve_item):
    resolve_item.return_value = (
        "file123",
        {"name": "Doc", "mimeType": "application/vnd.google-apps.document"},
    )
    service = _service()
    calls = []
    service._http.request.side_effect = lambda *a, **k: (
        calls.append("session") or _session_response()
    )
    service.files.return_value.update.return_value.execute.side_effect = lambda **k: (
        calls.append("metadata") or {}
    )

    result = await _unwrap(update_drive_file)(
        service=service,
        user_google_email="user@example.com",
        file_id="file123",
        name="Renamed",
        mime_type="text/markdown",  # the bytes to be uploaded, required for a native Doc
        return_upload_url=True,
    )

    # Metadata went through files().update without media and WITHOUT mimeType:
    # Drive rejects metadata mimeType changes on native files.
    update_kwargs = service.files.return_value.update.call_args.kwargs
    assert "media_body" not in update_kwargs
    assert update_kwargs["body"] == {"name": "Renamed"}
    (url,), kwargs = service._http.request.call_args
    assert url.startswith("https://www.googleapis.com/upload/drive/v3/files/file123?")
    assert kwargs["method"] == "PATCH"
    assert kwargs["headers"]["X-Upload-Content-Type"] == "text/markdown"
    assert UPLOAD_URL in result
    # Session first: it has no effect until the PUT, so a failed initiation can
    # never leave a half-applied rename/move/trash behind.
    assert calls == ["session", "metadata"]


@pytest.mark.asyncio
@patch("gdrive.drive_tools.resolve_drive_item", new_callable=AsyncMock)
async def test_update_initiation_failure_applies_no_metadata(resolve_item):
    resolve_item.return_value = (
        "f1",
        {"name": "notes.md", "mimeType": "text/markdown"},
    )
    service = _service(status=500)
    with pytest.raises(HttpError):
        await _unwrap(update_drive_file)(
            service=service,
            user_google_email="user@example.com",
            file_id="f1",
            name="Renamed",
            trashed=True,
            return_upload_url=True,
        )
    service.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
@patch("gdrive.drive_tools.resolve_drive_item", new_callable=AsyncMock)
@pytest.mark.parametrize(
    "current_mime, expected_body",
    [
        ("text/markdown", {"mimeType": "text/plain"}),
        (GOOGLE_DOC, None),
    ],
    ids=["non-native-keeps-mimeType", "native-drops-mimeType"],
)
async def test_update_metadata_mime_type_only_dropped_for_native(
    resolve_item, current_mime, expected_body
):
    """Non-native files get mimeType in the metadata body as the inline path sends
    it; only a native file (Drive rejects the change) drops it."""
    resolve_item.return_value = ("f1", {"name": "n", "mimeType": current_mime})
    service = _service()
    await _unwrap(update_drive_file)(
        service=service,
        user_google_email="user@example.com",
        file_id="f1",
        mime_type="text/plain",
        return_upload_url=True,
    )
    update = service.files.return_value.update
    if expected_body is None:
        update.assert_not_called()
    else:
        assert update.call_args.kwargs["body"] == expected_body
    assert (
        service._http.request.call_args.kwargs["headers"]["X-Upload-Content-Type"]
        == "text/plain"
    )


@pytest.mark.asyncio
@patch("gdrive.drive_tools.resolve_drive_item", new_callable=AsyncMock)
@pytest.mark.parametrize(
    "current_mime, upload_mime, message",
    [
        ("application/vnd.google-apps.form", "text/plain", "not supported for this"),
        (GOOGLE_DOC, "image/png", "Unsupported mime_type"),
    ],
    ids=["form-text", "doc-png"],
)
async def test_update_native_target_enforces_inline_import_rules(
    resolve_item, current_mime, upload_mime, message
):
    """Same rules as the inline replace path: the native target must be an editable
    Google type, and the upload's type must be in that type's import allowlist."""
    resolve_item.return_value = ("f1", {"name": "n", "mimeType": current_mime})
    service = _service()
    with pytest.raises(ValueError, match=message):
        await _unwrap(update_drive_file)(
            service=service,
            user_google_email="user@example.com",
            file_id="f1",
            name="Renamed",
            mime_type=upload_mime,
            return_upload_url=True,
        )
    service._http.request.assert_not_called()
    service.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
@patch("gdrive.drive_tools.resolve_drive_item", new_callable=AsyncMock)
async def test_update_normalises_upload_mime_type(resolve_item):
    resolve_item.return_value = ("doc1", {"name": "Doc", "mimeType": GOOGLE_DOC})
    service = _service()
    await _unwrap(update_drive_file)(
        service=service,
        user_google_email="user@example.com",
        file_id="doc1",
        mime_type=" Text/Markdown ",
        return_upload_url=True,
    )
    headers = service._http.request.call_args.kwargs["headers"]
    assert headers["X-Upload-Content-Type"] == "text/markdown"
    with pytest.raises(ValueError, match="native Google type"):
        await _unwrap(update_drive_file)(
            service=_service(),
            user_google_email="user@example.com",
            file_id="doc1",
            mime_type=" Application/vnd.google-apps.document",
            return_upload_url=True,
        )


@pytest.mark.asyncio
async def test_update_unsupported_mode_reported_before_flag_combination():
    with pytest.raises(ValueError, match="Unsupported mode"):
        await _unwrap(update_drive_file)(
            service=_service(),
            user_google_email="user@example.com",
            file_id="f1",
            mode="bogus",
            return_upload_url=True,
        )


@pytest.mark.asyncio
@patch("gdrive.drive_tools.resolve_drive_item", new_callable=AsyncMock)
async def test_update_without_metadata_skips_the_update_call(resolve_item):
    resolve_item.return_value = (
        "f1",
        {"name": "notes.md", "mimeType": "text/markdown"},
    )
    service = _service()

    await _unwrap(update_drive_file)(
        service=service,
        user_google_email="user@example.com",
        file_id="f1",
        return_upload_url=True,
    )

    service.files.return_value.update.assert_not_called()
    assert service._http.request.call_args.kwargs["method"] == "PATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["append", "prepend"])
async def test_update_rejects_non_replace_modes(mode):
    service = _service()
    with pytest.raises(ValueError, match="cannot be combined with mode"):
        await _unwrap(update_drive_file)(
            service=service,
            user_google_email="user@example.com",
            file_id="f1",
            content="more",
            mode=mode,
            return_upload_url=True,
        )
    service._http.request.assert_not_called()


@pytest.mark.asyncio
@patch("gdrive.drive_tools.resolve_drive_item", new_callable=AsyncMock)
@pytest.mark.parametrize(
    "extra",
    [
        {"content": "x"},
        {"file_path": "/tmp/x.docx"},
        {"file_url": "https://e/x"},
        {"source_format": "md"},
    ],
    ids=["content", "file_path", "file_url", "source_format"],
)
async def test_update_rejects_every_other_source(resolve_item, extra):
    resolve_item.return_value = ("f1", {"name": "n", "mimeType": "text/plain"})
    service = _service()
    with pytest.raises(ValueError, match="do not also pass"):
        await _unwrap(update_drive_file)(
            service=service,
            user_google_email="user@example.com",
            file_id="f1",
            return_upload_url=True,
            **extra,
        )
    service._http.request.assert_not_called()
    resolve_item.assert_not_called()  # argument checks precede any Drive I/O


@pytest.mark.asyncio
@patch("gdrive.drive_tools.resolve_drive_item", new_callable=AsyncMock)
async def test_update_native_file_requires_explicit_upload_mime(resolve_item):
    """A native Doc's own mimeType is the conversion TARGET, never a description of
    the uploaded bytes; omitting mime_type must fail here, not at Google."""
    resolve_item.return_value = (
        "doc1",
        {"name": "Doc", "mimeType": "application/vnd.google-apps.document"},
    )
    service = _service()
    with pytest.raises(ValueError, match="native Google type"):
        await _unwrap(update_drive_file)(
            service=service,
            user_google_email="user@example.com",
            file_id="doc1",
            return_upload_url=True,
        )
    service._http.request.assert_not_called()
    service.files.return_value.update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, source_format, source_mime, target_mime",
    [
        (import_to_google_doc, "docx", DOCX, "application/vnd.google-apps.document"),
        (
            import_to_google_sheets,
            "csv",
            "text/csv",
            "application/vnd.google-apps.spreadsheet",
        ),
        (
            import_to_google_slides,
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.google-apps.presentation",
        ),
    ],
    ids=["doc", "sheets", "slides"],
)
async def test_import_session_converts_source_to_target(
    folder, tool, source_format, source_mime, target_mime
):
    service = _service()

    result = await _unwrap(tool)(
        service=service,
        user_google_email="user@example.com",
        file_name="Report.bin",
        source_format=source_format,
        return_upload_url=True,
    )

    (url,), kwargs = service._http.request.call_args
    assert kwargs["method"] == "POST" and "uploadType=resumable" in url
    body = json.loads(kwargs["body"])
    assert body == {"name": "Report", "parents": ["folder123"], "mimeType": target_mime}
    assert kwargs["headers"]["X-Upload-Content-Type"] == source_mime
    assert UPLOAD_URL in result


@pytest.mark.asyncio
async def test_import_requires_source_format():
    service = _service()
    with pytest.raises(ValueError, match="source_format is required"):
        await _unwrap(import_to_google_doc)(
            service=service,
            user_google_email="user@example.com",
            file_name="Report",
            return_upload_url=True,
        )
    service._http.request.assert_not_called()


@pytest.mark.asyncio
async def test_import_rejects_unsupported_source_format():
    with pytest.raises(ValueError, match="Unsupported source_format"):
        await _unwrap(import_to_google_slides)(
            service=_service(),
            user_google_email="user@example.com",
            file_name="Deck",
            source_format="xlsx",
            return_upload_url=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"content": "# hi"},
        {"file_path": "/tmp/r.docx"},
        {"file_url": "https://example.com/r.docx"},
        {"base64_content": "aGVsbG8="},
    ],
    ids=["content", "file_path", "file_url", "base64_content"],
)
async def test_import_rejects_every_other_source(extra):
    """base64_content included: an upload URL with silently ignored inline bytes
    would look like success while uploading nothing."""
    service = _service()
    with pytest.raises(ValueError, match="do not also pass"):
        await _unwrap(import_to_google_doc)(
            service=service,
            user_google_email="user@example.com",
            file_name="doc.docx",
            source_format="docx",
            return_upload_url=True,
            **extra,
        )
    service._http.request.assert_not_called()


@pytest.mark.asyncio
async def test_session_initiation_http_error_keeps_googles_reason(folder):
    """Non-2xx initiation raises HttpError, so Google's reason reaches the caller
    and handle_http_errors adds its 401/403 guidance as for any Drive call."""
    body = {
        "error": {
            "code": 403,
            "message": "The user's Drive storage quota has been exceeded.",
            "errors": [{"reason": "storageQuotaExceeded"}],
        }
    }
    service = Mock()
    service._http.request.return_value = (
        httplib2.Response({"status": "403"}),
        json.dumps(body).encode(),
    )
    tool = handle_http_errors("create_drive_file", service_type="drive")(
        _unwrap(create_drive_file)
    )
    with pytest.raises(Exception) as excinfo:
        await tool(
            service=service,
            user_google_email="user@example.com",
            file_name="report.pdf",
            mime_type="application/pdf",
            return_upload_url=True,
        )
    message = str(excinfo.value)
    assert "storage quota has been exceeded" in message
    assert "re-authenticate" in message
    assert isinstance(excinfo.value.__cause__, HttpError)


@pytest.mark.asyncio
async def test_session_initiation_errors_are_reported():
    with pytest.raises(HttpError, match="403"):
        await _initiate_resumable_upload_session(
            _service(status=403), upload_mime_type="text/plain", file_metadata={}
        )
    with pytest.raises(Exception, match="no session URL"):
        await _initiate_resumable_upload_session(
            _service(location=None), upload_mime_type="text/plain", file_metadata={}
        )
