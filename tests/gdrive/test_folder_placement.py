"""Tests for the shared Drive folder-placement helpers.

``place_file_in_folder`` always moves the file; callers decide whether a move is
wanted. The "no move for root" behaviour therefore lives with the create tools
(see tests/gdocs/test_create_doc_folder.py and
tests/gsheets/test_create_spreadsheet_folder.py).
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from auth.scopes import DRIVE_FILE_SCOPE
from gdrive.drive_helpers import (
    folder_move_failed_note,
    folder_note,
    place_created_file_in_folder,
    place_file_in_folder,
)


def _drive_mock(parents=("root",), *, get_response=None):
    """Drive service mock exposing files().get() and files().update()."""
    service = Mock()
    service.files().get().execute = Mock(
        return_value={"parents": list(parents)}
        if get_response is None
        else get_response
    )
    service.files().update().execute = Mock(
        return_value={"id": "file-1", "parents": ["resolved-folder"]}
    )
    return service


def _patch_resolve(return_value="resolved-folder", side_effect=None):
    return patch(
        "gdrive.drive_helpers.resolve_folder_id",
        new=AsyncMock(return_value=return_value, side_effect=side_effect),
    )


@pytest.mark.asyncio
async def test_place_file_in_folder_moves_file_and_returns_resolved_id():
    service = _drive_mock(parents=["root"])

    with _patch_resolve("resolved-folder") as resolve:
        result = await place_file_in_folder(service, "file-1", "folder-abc")

    assert result == "resolved-folder"
    resolve.assert_awaited_once_with(service, "folder-abc")

    get_kwargs = service.files().get.call_args.kwargs
    assert get_kwargs == {
        "fileId": "file-1",
        "fields": "parents",
        "supportsAllDrives": True,
    }

    update_kwargs = service.files().update.call_args.kwargs
    assert update_kwargs == {
        "fileId": "file-1",
        "addParents": "resolved-folder",
        "removeParents": "root",
        "fields": "id, parents",
        "supportsAllDrives": True,
    }


@pytest.mark.asyncio
async def test_place_file_in_folder_removes_every_existing_parent():
    service = _drive_mock(parents=["root", "other-folder"])

    with _patch_resolve():
        await place_file_in_folder(service, "file-1", "folder-abc")

    assert (
        service.files().update.call_args.kwargs["removeParents"] == "root,other-folder"
    )


@pytest.mark.asyncio
async def test_place_file_in_folder_tolerates_missing_parents_field():
    service = _drive_mock(get_response={})

    with _patch_resolve():
        await place_file_in_folder(service, "file-1", "folder-abc")

    assert service.files().update.call_args.kwargs["removeParents"] == ""


@pytest.mark.asyncio
async def test_place_file_in_folder_resolves_shortcut_before_moving():
    """The raw folder_id goes to resolve_folder_id; the resolved ID is moved into."""
    service = _drive_mock()

    with _patch_resolve("target-folder") as resolve:
        await place_file_in_folder(service, "file-1", "shortcut-id")

    resolve.assert_awaited_once_with(service, "shortcut-id")
    assert service.files().update.call_args.kwargs["addParents"] == "target-folder"


@pytest.mark.asyncio
async def test_place_file_in_folder_does_not_move_when_resolve_fails():
    """A non-folder or missing destination must fail before the file is touched."""
    service = _drive_mock()
    service.files().get.reset_mock()
    service.files().update.reset_mock()

    with _patch_resolve(side_effect=Exception("is not a folder")):
        with pytest.raises(Exception, match="is not a folder"):
            await place_file_in_folder(service, "file-1", "not-a-folder")

    service.files().get.assert_not_called()
    service.files().update.assert_not_called()


@pytest.mark.asyncio
async def test_place_file_in_folder_does_not_remove_the_destination():
    """A caller naming the file's current parent must not add and remove it at once."""
    service = _drive_mock(parents=["resolved-folder", "other-folder"])

    with _patch_resolve("resolved-folder"):
        await place_file_in_folder(service, "file-1", "resolved-folder")

    update_kwargs = service.files().update.call_args.kwargs
    assert update_kwargs["addParents"] == "resolved-folder"
    assert update_kwargs["removeParents"] == "other-folder"


@pytest.mark.asyncio
async def test_place_file_in_folder_sends_no_remove_when_already_in_place():
    service = _drive_mock(parents=["resolved-folder"])

    with _patch_resolve("resolved-folder"):
        await place_file_in_folder(service, "file-1", "resolved-folder")

    assert service.files().update.call_args.kwargs["removeParents"] == ""


@pytest.mark.parametrize("folder_id", ["root", "", None])
def test_folder_note_empty_for_root(folder_id):
    assert folder_note(folder_id) == ""


def test_folder_move_failed_note_names_folder_and_reason():
    note = folder_move_failed_note("folder-abc", Exception("is not a folder"))

    assert "folder-abc" in note
    assert "is not a folder" in note
    assert "My Drive root" in note


def test_folder_note_names_destination():
    assert folder_note("folder-abc") == " Placed in folder 'folder-abc'."


def test_placement_still_requires_drive_file_scope():
    """
    Moving the Drive requirement off the create tools must not drop it.

    create_doc and create_spreadsheet no longer declare drive.file, so this
    helper is the only thing standing between a Sheets- or Docs-only credential
    and arbitrary-folder placement. It authenticates Drive itself, which fails
    closed: without drive.file the move raises and the file stays in root.
    """
    assert place_created_file_in_folder._required_google_scopes == [DRIVE_FILE_SCOPE]


@pytest.mark.asyncio
async def test_place_created_file_in_folder_delegates_to_the_move():
    """The decorated wrapper adds authentication and nothing else."""
    service = _drive_mock(parents=["root"])

    with _patch_resolve("resolved-folder"):
        resolved = await place_created_file_in_folder.__wrapped__(
            service,
            user_google_email="user@example.com",
            file_id="file-1",
            folder_id="folder-abc",
            tool_name="create_doc",
        )

    assert resolved == "resolved-folder"
    assert service.files().update.call_args.kwargs["addParents"] == "resolved-folder"
