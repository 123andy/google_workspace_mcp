"""The upload tools with signed upload links on: the session is opened as before,
but the caller gets a ``/ul/`` link on this server, never Google's URL."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from google.oauth2.credentials import Credentials
from key_value.aio.stores.memory import MemoryStore

import core.signed_downloads as sd
import core.upload_relay as ur
from core.config import get_transport_mode
from core.server import set_transport_mode
from gdrive.drive_helpers import upload_url_hint
from gdrive.drive_tools import (
    create_drive_file,
    import_to_google_doc,
    import_to_google_sheets,
    import_to_google_slides,
    update_drive_file,
)

UPLOAD_URL = (
    "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=X"
)
EXTERNAL = "https://mcp.example.com"
USER = "user@example.com"


def _unwrap(tool):
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _service(location=UPLOAD_URL):
    response = Mock()
    response.status = 200
    response.get = Mock(return_value=location)
    service = Mock()
    service._http.request.return_value = (response, b"")
    return service


@pytest.fixture
def relay_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", "true")
    monkeypatch.setenv(sd.FLAG_ENV, "true")
    monkeypatch.setenv(ur.FLAG_ENV, "true")
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", EXTERNAL)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "relay-tools-secret")
    monkeypatch.delenv(ur.MAX_BYTES_ENV, raising=False)
    sd._signing_key.cache_clear()
    store = MemoryStore()
    monkeypatch.setattr(ur, "_store", store)
    monkeypatch.setattr(ur, "_store_built", True)
    monkeypatch.setattr(
        sd, "_recover_credentials", Mock(return_value=Credentials("t", "r"))
    )
    saved = get_transport_mode()
    set_transport_mode("streamable-http")
    yield store
    set_transport_mode(saved)
    sd._signing_key.cache_clear()


@pytest.fixture
def folder():
    resolve = AsyncMock(return_value="folder123")
    with (
        patch("gdrive.drive_tools.resolve_folder_id", resolve),
        patch("gdrive.drive_helpers.resolve_folder_id", resolve),
    ):
        yield resolve


async def _stored(store, text):
    handle = text.split(f"{EXTERNAL}/ul/")[1].split()[0].strip("'")
    return await store.get(handle, collection=ur._COLLECTION)


def _assert_link_only(text):
    assert f"{EXTERNAL}/ul/" in text
    assert "googleapis.com" not in text and "upload_id" not in text
    assert not text.startswith("Error")


@pytest.mark.asyncio
async def test_create_returns_a_relay_link(relay_on, folder):
    service = _service()
    text = await _unwrap(create_drive_file)(
        service=service,
        user_google_email=USER,
        file_name="report.pdf",
        mime_type="application/pdf",
        return_upload_url=True,
    )
    _assert_link_only(text)
    assert text.startswith("Resumable upload session created for new file")
    record = await _stored(relay_on, text)
    assert (record["gurl"], record["sub"], record["mt"]) == (
        UPLOAD_URL,
        USER,
        "application/pdf",
    )
    # The session itself is opened exactly as before.
    (url, method), kwargs = service._http.request.call_args
    assert method == "POST" and json.loads(kwargs["body"])["name"] == "report.pdf"


@pytest.mark.asyncio
async def test_update_returns_a_relay_link(relay_on):
    with patch(
        "gdrive.drive_tools.resolve_drive_item",
        AsyncMock(return_value=("file123", {"name": "N", "mimeType": "text/plain"})),
    ):
        text = await _unwrap(update_drive_file)(
            service=_service(),
            user_google_email=USER,
            file_id="file123",
            mime_type="text/plain",
            return_upload_url=True,
        )
    _assert_link_only(text)
    assert (await _stored(relay_on, text))["mt"] == "text/plain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, source_format, source_mime",
    [
        (
            import_to_google_doc,
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (import_to_google_sheets, "csv", "text/csv"),
        (
            import_to_google_slides,
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    ],
    ids=["doc", "sheets", "slides"],
)
async def test_imports_return_a_relay_link_for_the_source_type(
    relay_on, folder, tool, source_format, source_mime
):
    text = await _unwrap(tool)(
        service=_service(),
        user_google_email=USER,
        file_name="Report",
        source_format=source_format,
        return_upload_url=True,
    )
    _assert_link_only(text)
    assert (await _stored(relay_on, text))["mt"] == source_mime


@pytest.mark.asyncio
async def test_on_but_unusable_is_an_error_not_googles_url(
    relay_on, folder, monkeypatch
):
    monkeypatch.delenv(sd.FLAG_ENV, raising=False)
    text = await _unwrap(create_drive_file)(
        service=_service(),
        user_google_email=USER,
        file_name="report.pdf",
        mime_type="application/pdf",
        return_upload_url=True,
    )
    assert text.startswith("Error:")
    assert "googleapis.com" not in text and "/ul/" not in text


@pytest.mark.asyncio
async def test_unexpected_session_url_from_google_is_an_error(relay_on, folder):
    text = await _unwrap(create_drive_file)(
        service=_service(location="https://evil.example/upload?upload_id=X"),
        user_google_email=USER,
        file_name="report.pdf",
        mime_type="application/pdf",
        return_upload_url=True,
    )
    assert text.startswith("Error:") and "evil" not in text


@pytest.mark.asyncio
async def test_still_refused_where_local_files_work(relay_on, monkeypatch):
    """The relay changes what the URL is, not where return_upload_url is offered."""
    from core.utils import UserInputError

    monkeypatch.delenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", raising=False)
    with patch("core.utils.is_stateless_mode", return_value=False):
        with pytest.raises(UserInputError):
            await _unwrap(create_drive_file)(
                service=_service(),
                user_google_email=USER,
                file_name="report.pdf",
                mime_type="application/pdf",
                return_upload_url=True,
            )


def test_hint_text_off_is_unchanged(monkeypatch):
    monkeypatch.delenv(ur.FLAG_ENV, raising=False)
    assert upload_url_hint() == (
        "If the file is on the caller's machine, call this tool again with "
        "return_upload_url=true and mime_type, then PUT the bytes to the URL it "
        "returns (suited to large or binary files; only offered where local file "
        "access is disabled)."
    )


def test_hint_text_on_names_this_server(relay_on):
    hint = upload_url_hint()
    assert "the upload URL it returns on this server" in hint
    assert "googleapis" not in hint


@pytest.mark.parametrize(
    "name",
    [
        "create_drive_file",
        "update_drive_file",
        "import_to_google_doc",
        "import_to_google_sheets",
        "import_to_google_slides",
    ],
)
def test_flag_on_rewrites_each_upload_tools_description(monkeypatch, name):
    """Pins the text describe() relies on, so a wording change upstream fails here
    instead of silently leaving 'a Google ... URL' in the description."""
    import gdrive.drive_tools as drive_tools

    tool = getattr(drive_tools, name)
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    assert ur._DOC_FROM in fn.__doc__  # flag off at import: untouched

    monkeypatch.setenv(ur.FLAG_ENV, "true")

    def copy():
        pass

    copy.__doc__ = fn.__doc__
    ur.describe(copy)
    assert ur._DOC_FROM not in copy.__doc__
    assert "upload URL on this server" in copy.__doc__
    assert "directly (no Authorization header)" not in copy.__doc__
    assert copy.__doc__.count("return_upload_url (bool)") == 1


def test_tool_doc_rewrite_is_registered_on_each_tool():
    import gdrive.drive_tools as drive_tools
    import inspect

    source = inspect.getsource(drive_tools)
    for name in (
        "create_drive_file",
        "update_drive_file",
        "import_to_google_doc",
        "import_to_google_sheets",
        "import_to_google_slides",
    ):
        assert f"@upload_relay.describe\nasync def {name}(" in source
