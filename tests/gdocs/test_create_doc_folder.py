"""Tests for create_doc folder placement (folder_id parameter)."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.server import server
from core.tool_registry import get_tool_components
from gdocs.docs_tools import create_doc


def _docs_mock(document_id="doc-123"):
    """Docs service mock for documents().create() and documents().batchUpdate()."""
    service = Mock()
    service.documents().create().execute = Mock(
        return_value={"documentId": document_id}
    )
    service.documents().batchUpdate().execute = Mock(return_value={})
    return service


def _drive_mock(parents=("root",)):
    service = Mock()
    service.files().get().execute = Mock(return_value={"parents": list(parents)})
    service.files().update().execute = Mock(return_value={"id": "doc-123"})
    return service


def _patch_resolve(return_value="resolved-folder"):
    return patch(
        "gdrive.drive_helpers.resolve_folder_id",
        new=AsyncMock(return_value=return_value),
    )


async def _call_create_doc(docs_service, drive_service, **overrides):
    """Call the undecorated implementation to keep auth out of unit tests."""
    impl = create_doc.__wrapped__.__wrapped__
    defaults = {
        "docs_service": docs_service,
        "drive_service": drive_service,
        "user_google_email": "user@example.com",
        "title": "My Doc",
    }
    defaults.update(overrides)
    return await impl(**defaults)


def test_create_doc_schema_exposes_optional_folder_id():
    components = get_tool_components(server)
    parameters = components["create_doc"].parameters

    assert "folder_id" not in parameters["required"]
    assert parameters["properties"]["folder_id"]["default"] == "root"


@pytest.mark.asyncio
async def test_create_doc_defaults_to_root_and_skips_drive():
    docs_service = _docs_mock()
    drive_service = Mock()

    with _patch_resolve() as resolve:
        result = await _call_create_doc(docs_service, drive_service)

    resolve.assert_not_awaited()
    drive_service.files.assert_not_called()
    assert "Placed in folder" not in result
    assert "Created Google Doc 'My Doc' (ID: doc-123)" in result
    assert "https://docs.google.com/document/d/doc-123/edit" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("folder_id", ["root", "", None])
async def test_create_doc_skips_the_move_for_root_folder_ids(folder_id):
    """No Drive round-trip when the caller is not asking for a real folder."""
    docs_service = _docs_mock()
    drive_service = Mock()

    with _patch_resolve() as resolve:
        result = await _call_create_doc(
            docs_service, drive_service, folder_id=folder_id
        )

    resolve.assert_not_awaited()
    drive_service.files.assert_not_called()
    assert "Placed in folder" not in result


@pytest.mark.asyncio
async def test_create_doc_moves_new_doc_into_requested_folder():
    docs_service = _docs_mock()
    drive_service = _drive_mock(parents=["root"])

    with _patch_resolve("resolved-folder") as resolve:
        result = await _call_create_doc(
            docs_service, drive_service, folder_id="folder-abc"
        )

    resolve.assert_awaited_once_with(drive_service, "folder-abc")
    assert drive_service.files().get.call_args.kwargs["fileId"] == "doc-123"
    update_kwargs = drive_service.files().update.call_args.kwargs
    assert update_kwargs["fileId"] == "doc-123"
    assert update_kwargs["addParents"] == "resolved-folder"
    assert update_kwargs["removeParents"] == "root"
    assert update_kwargs["supportsAllDrives"] is True
    assert "Placed in folder 'folder-abc'." in result


@pytest.mark.asyncio
async def test_create_doc_still_creates_doc_with_plain_title_body():
    """The Docs create call must stay title-only; the folder is a Drive concern."""
    docs_service = _docs_mock()
    drive_service = _drive_mock()

    with _patch_resolve():
        await _call_create_doc(docs_service, drive_service, folder_id="folder-abc")

    assert docs_service.documents().create.call_args.kwargs["body"] == {
        "title": "My Doc"
    }


@pytest.mark.asyncio
async def test_create_doc_inserts_content_after_moving():
    docs_service = _docs_mock()
    drive_service = _drive_mock()

    with _patch_resolve():
        result = await _call_create_doc(
            docs_service, drive_service, content="Hello", folder_id="folder-abc"
        )

    batch_kwargs = docs_service.documents().batchUpdate.call_args.kwargs
    assert batch_kwargs["documentId"] == "doc-123"
    assert batch_kwargs["body"]["requests"] == [
        {"insertText": {"location": {"index": 1}, "text": "Hello"}}
    ]
    assert "Initial content: 5 characters inserted." in result
    assert "Placed in folder 'folder-abc'." in result


@pytest.mark.asyncio
async def test_create_doc_without_content_skips_batch_update():
    docs_service = _docs_mock()
    drive_service = _drive_mock()
    docs_service.documents().batchUpdate.reset_mock()

    with _patch_resolve():
        result = await _call_create_doc(
            docs_service, drive_service, folder_id="folder-abc"
        )

    docs_service.documents().batchUpdate.assert_not_called()
    assert "Document is empty" in result


@pytest.mark.asyncio
async def test_create_doc_reports_invalid_folder_without_orphaning_the_doc():
    """A failed move must still surface the doc ID, or the new doc is unreachable."""
    docs_service = _docs_mock()
    drive_service = _drive_mock()
    drive_service.files().update.reset_mock()

    with patch(
        "gdrive.drive_helpers.resolve_folder_id",
        new=AsyncMock(side_effect=Exception("is not a folder")),
    ):
        result = await _call_create_doc(
            docs_service, drive_service, folder_id="not-a-folder"
        )

    drive_service.files().update.assert_not_called()
    assert "doc-123" in result
    assert "is not a folder" in result
    assert "My Drive root" in result
    assert "Placed in folder" not in result
