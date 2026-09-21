"""Tests for create_spreadsheet folder placement (folder_id parameter)."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.server import server
from core.tool_registry import get_tool_components
from gsheets.sheets_tools import create_spreadsheet


def _sheets_mock(spreadsheet_id="sheet-123"):
    """Sheets service mock for spreadsheets().create()."""
    service = Mock()
    service.spreadsheets().create().execute = Mock(
        return_value={
            "spreadsheetId": spreadsheet_id,
            "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
            "properties": {"title": "My Sheet", "locale": "en_US"},
        }
    )
    return service


def _drive_mock(parents=("root",)):
    service = Mock()
    service.files().get().execute = Mock(return_value={"parents": list(parents)})
    service.files().update().execute = Mock(return_value={"id": "sheet-123"})
    return service


def _patch_resolve(return_value="resolved-folder"):
    return patch(
        "gdrive.drive_helpers.resolve_folder_id",
        new=AsyncMock(return_value=return_value),
    )


async def _call_create_spreadsheet(sheets_service, drive_service, **overrides):
    """Call the undecorated implementation to keep auth out of unit tests."""
    impl = create_spreadsheet.__wrapped__.__wrapped__
    defaults = {
        "sheets_service": sheets_service,
        "drive_service": drive_service,
        "user_google_email": "user@example.com",
        "title": "My Sheet",
    }
    defaults.update(overrides)
    return await impl(**defaults)


def test_create_spreadsheet_schema_exposes_optional_folder_id():
    components = get_tool_components(server)
    parameters = components["create_spreadsheet"].parameters

    assert "folder_id" not in parameters["required"]
    assert parameters["properties"]["folder_id"]["default"] == "root"


@pytest.mark.asyncio
async def test_create_spreadsheet_defaults_to_root_and_skips_drive():
    sheets_service = _sheets_mock()
    drive_service = Mock()

    with _patch_resolve() as resolve:
        result = await _call_create_spreadsheet(sheets_service, drive_service)

    resolve.assert_not_awaited()
    drive_service.files.assert_not_called()
    assert "Placed in folder" not in result
    assert "Successfully created spreadsheet 'My Sheet' for user@example.com." in result
    assert "ID: sheet-123" in result
    assert "Locale: en_US" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("folder_id", ["root", "", None])
async def test_create_spreadsheet_skips_the_move_for_root_folder_ids(folder_id):
    """No Drive round-trip when the caller is not asking for a real folder."""
    sheets_service = _sheets_mock()
    drive_service = Mock()

    with _patch_resolve() as resolve:
        result = await _call_create_spreadsheet(
            sheets_service, drive_service, folder_id=folder_id
        )

    resolve.assert_not_awaited()
    drive_service.files.assert_not_called()
    assert "Placed in folder" not in result


@pytest.mark.asyncio
async def test_create_spreadsheet_moves_new_file_into_requested_folder():
    sheets_service = _sheets_mock()
    drive_service = _drive_mock(parents=["root"])

    with _patch_resolve("resolved-folder") as resolve:
        result = await _call_create_spreadsheet(
            sheets_service, drive_service, folder_id="folder-abc"
        )

    resolve.assert_awaited_once_with(drive_service, "folder-abc")
    assert drive_service.files().get.call_args.kwargs["fileId"] == "sheet-123"
    update_kwargs = drive_service.files().update.call_args.kwargs
    assert update_kwargs["fileId"] == "sheet-123"
    assert update_kwargs["addParents"] == "resolved-folder"
    assert update_kwargs["removeParents"] == "root"
    assert update_kwargs["supportsAllDrives"] is True
    assert "Placed in folder 'folder-abc'." in result


@pytest.mark.asyncio
async def test_create_spreadsheet_keeps_sheet_names_in_create_body():
    sheets_service = _sheets_mock()
    drive_service = _drive_mock()

    with _patch_resolve():
        await _call_create_spreadsheet(
            sheets_service,
            drive_service,
            sheet_names=["Q1", "Q2"],
            folder_id="folder-abc",
        )

    body = sheets_service.spreadsheets().create.call_args.kwargs["body"]
    assert body["properties"] == {"title": "My Sheet"}
    assert body["sheets"] == [
        {"properties": {"title": "Q1"}},
        {"properties": {"title": "Q2"}},
    ]


@pytest.mark.asyncio
async def test_create_spreadsheet_omits_sheets_key_without_sheet_names():
    sheets_service = _sheets_mock()
    drive_service = Mock()

    with _patch_resolve():
        await _call_create_spreadsheet(sheets_service, drive_service)

    assert "sheets" not in sheets_service.spreadsheets().create.call_args.kwargs["body"]


@pytest.mark.asyncio
async def test_create_spreadsheet_reports_invalid_folder_without_orphaning_the_file():
    """A failed move must still surface the spreadsheet ID and URL."""
    sheets_service = _sheets_mock()
    drive_service = _drive_mock()
    drive_service.files().update.reset_mock()

    with patch(
        "gdrive.drive_helpers.resolve_folder_id",
        new=AsyncMock(side_effect=Exception("is not a folder")),
    ):
        result = await _call_create_spreadsheet(
            sheets_service, drive_service, folder_id="not-a-folder"
        )

    drive_service.files().update.assert_not_called()
    assert "is not a folder" in result
    assert "My Drive root" in result
    assert "Placed in folder" not in result
