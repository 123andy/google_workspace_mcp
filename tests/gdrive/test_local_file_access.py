"""Opt-in disabling of server-side file paths.

'file_path' resolves on the machine the SERVER runs on. That works for stdio and
for streamable-http on localhost, but not for a hosted deployment with no view of
the caller's disk. Operators of such deployments set
WORKSPACE_MCP_DISABLE_LOCAL_FILES=true (stateless mode implies it), which hides
file_path from tool schemas and rejects it at runtime with tool-specific advice.
Transport alone never disables it.
"""

import inspect
import os
import re
import subprocess
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import (  # noqa: E402
    UserInputError,
    local_file_access_enabled,
    local_file_args,
    validate_file_path,
)
from gdrive.drive_tools import (  # noqa: E402
    import_to_google_doc,
    import_to_google_sheets,
    import_to_google_slides,
    update_drive_file,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
DISABLED = patch("gdrive.drive_tools.local_file_access_enabled", return_value=False)


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _run_subprocess(code: str, extra_env: dict[str, str]) -> str:
    # Modes that reshape tool signatures (OAuth 2.1 drops user_google_email) or
    # toggle local file access must not leak in from the developer's shell.
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "MCP_ENABLE_OAUTH21",
            "EXTERNAL_OAUTH21_PROVIDER",
            "WORKSPACE_MCP_STATELESS_MODE",
            "MCP_SINGLE_USER_MODE",
            "WORKSPACE_MCP_DISABLE_LOCAL_FILES",
        )
    }
    env.update(
        GOOGLE_OAUTH_CLIENT_ID="test-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="test-client-secret",
        **extra_env,
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


class TestLocalFileAccessSetting:
    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", raising=False)
        with patch("core.utils.is_stateless_mode", return_value=False):
            assert local_file_access_enabled()
            assert local_file_args("file_path") is None

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", "true")
        with patch("core.utils.is_stateless_mode", return_value=False):
            assert not local_file_access_enabled()
            assert local_file_args("a", "b") == ["a", "b"]

    def test_disabled_by_stateless_mode(self, monkeypatch):
        monkeypatch.delenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", raising=False)
        with patch("core.utils.is_stateless_mode", return_value=True):
            assert not local_file_access_enabled()

    @patch("core.utils.get_transport_mode", return_value="streamable-http")
    def test_transport_alone_does_not_disable(self, _mode, monkeypatch):
        monkeypatch.delenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", raising=False)
        with patch("core.utils.is_stateless_mode", return_value=False):
            assert local_file_access_enabled()


class TestFilePathRejectedWhenDisabled:
    """Guarded tools reject file_path BEFORE touching the Drive API, with an
    error that names only the routes that tool really has."""

    @pytest.mark.asyncio
    @DISABLED
    @pytest.mark.parametrize(
        "tool, kwargs",
        [
            (import_to_google_doc, {"file_name": "n"}),
            (import_to_google_slides, {"file_name": "n"}),
            (import_to_google_sheets, {"file_name": "n"}),
            (update_drive_file, {"file_id": "abc123"}),
        ],
        ids=["doc", "slides", "sheets", "update"],
    )
    async def test_error_names_exactly_the_routes_the_tool_has(
        self, _enabled, tool, kwargs
    ):
        """Routes are read from each tool's real signature, so adding or
        removing one of these parameters fails here instead of leaving the
        guidance quietly wrong."""
        fn = _unwrap(tool)
        has = {"content", "base64_content", "file_url", "return_upload_url"} & set(
            inspect.signature(fn).parameters
        )
        service = Mock()

        with pytest.raises(UserInputError) as exc:
            await fn(
                service=service,
                user_google_email="user@example.com",
                file_path="/Users/someone/file.bin",
                **kwargs,
            )
        msg = str(exc.value)
        assert "local file access is disabled" in msg
        advice = msg.split("Instead,", 1)[1]
        assert set(re.findall(r"'(\w+)'", advice)) == has
        service.files.assert_not_called()

    @pytest.mark.asyncio
    @DISABLED
    async def test_update_offers_content_for_text_formats_only(self, _enabled):
        """update_drive_file has no base64_content, and its 'content' rejects
        binary formats, so a .docx caller must not be sent round in a circle."""
        with pytest.raises(UserInputError) as exc:
            await _unwrap(update_drive_file)(
                service=Mock(),
                user_google_email="user@example.com",
                file_id="abc123",
                file_path="/Users/someone/report.docx",
            )
        assert "for text formats, send it inline via 'content'" in str(exc.value)

    @pytest.mark.asyncio
    @DISABLED
    async def test_update_guard_answers_before_the_mode_check(self, _enabled):
        """The mode check's own advice names file_path, so the guard runs first."""
        with pytest.raises(UserInputError, match="local file access is disabled"):
            await _unwrap(update_drive_file)(
                service=Mock(),
                user_google_email="user@example.com",
                file_id="abc123",
                file_path="/Users/someone/notes.md",
                mode="append",
            )

    @pytest.mark.asyncio
    @DISABLED
    @patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock)
    async def test_inline_content_still_works(self, mock_folder, _enabled):
        mock_folder.return_value = "root"
        service = Mock()
        service.files().create().execute.return_value = {
            "id": "doc1",
            "name": "Notes",
            "webViewLink": "https://docs.google.com/doc1",
            "mimeType": "application/vnd.google-apps.document",
        }
        service.files().create.reset_mock()

        result = await _unwrap(import_to_google_doc)(
            service=service,
            user_google_email="user@example.com",
            file_name="Notes.md",
            content="# Title\n\nHello",
        )

        service.files().create.assert_called_once()
        assert "Successfully imported" in result


class TestFilePathWorksByDefault:
    @pytest.mark.asyncio
    @patch("core.utils.get_transport_mode", return_value="streamable-http")
    @patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock)
    async def test_localhost_http_keeps_file_path(
        self, mock_folder, _mode, tmp_path, monkeypatch
    ):
        """streamable-http on localhost shares the caller's filesystem, so the
        file_path route works end to end without any opt-in."""
        monkeypatch.delenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", raising=False)
        monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))
        src = tmp_path / "notes.md"
        src.write_text("# Title\n\nHello")
        mock_folder.return_value = "root"
        service = Mock()
        service.files().create().execute.return_value = {
            "id": "doc1",
            "name": "notes",
            "webViewLink": "https://docs.google.com/doc1",
            "mimeType": "application/vnd.google-apps.document",
        }
        service.files().create.reset_mock()

        with patch("core.utils.is_stateless_mode", return_value=False):
            result = await _unwrap(import_to_google_doc)(
                service=service,
                user_google_email="user@example.com",
                file_name="notes.md",
                file_path=str(src),
            )

        service.files().create.assert_called_once()
        assert "Successfully imported" in result


class TestValidateFilePath:
    def test_refuses_every_path_when_disabled(self, tmp_path, monkeypatch):
        """Covers call sites without their own guard (create_drive_file's
        file:// URLs, Gmail attachment paths)."""
        monkeypatch.setenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", "true")
        with pytest.raises(UserInputError, match="Local file access is disabled"):
            validate_file_path(str(tmp_path))

    @patch("core.utils.get_transport_mode", return_value="streamable-http")
    def test_missing_path_over_http_hints_at_the_server_boundary(
        self, _mode, monkeypatch
    ):
        monkeypatch.delenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", raising=False)
        with patch("core.utils.is_stateless_mode", return_value=False):
            with pytest.raises(FileNotFoundError) as exc:
                validate_file_path("/definitely/not/here.md")
        assert "different machine" in str(exc.value)

    @patch("core.utils.get_transport_mode", return_value="stdio")
    def test_missing_path_on_stdio_stays_plain(self, _mode, monkeypatch):
        monkeypatch.delenv("WORKSPACE_MCP_DISABLE_LOCAL_FILES", raising=False)
        with patch("core.utils.is_stateless_mode", return_value=False):
            with pytest.raises(FileNotFoundError) as exc:
                validate_file_path("/definitely/not/here.md")
        assert "different machine" not in str(exc.value)


class TestSchemaThroughFastMCP:
    """exclude_args is fixed at decoration time and FastMCP only validates it
    when non-None, so an in-process suite under the default can never catch a
    stale exclusion. It also hides file_path from the ADVERTISED schema only:
    a client with a cached schema can still send it, and the guard is what
    answers. Go through a real client in a subprocess to cover both."""

    CODE = """
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from core.server import server, set_transport_mode
set_transport_mode('streamable-http')
import auth.service_decorator as sd
import gdrive.drive_tools
from fastmcp import Client

async def main():
    auth = AsyncMock(return_value=(MagicMock(), 'user@example.com'))
    with patch.object(sd, '_authenticate_service', auth):
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
            schema = tools['import_to_google_slides'].inputSchema
            print('ADVERTISED:' + str('file_path' in schema['properties']))
            result = await client.call_tool(
                'import_to_google_slides',
                {'file_name': 'Deck', 'file_path': '/Users/someone/deck.pptx',
                 'user_google_email': 'user@example.com'},
                raise_on_error=False,
            )
            print('RESULT:' + result.content[0].text)

asyncio.run(main())
"""

    def test_disabled_hides_file_path_and_guards_stale_clients(self):
        out = _run_subprocess(self.CODE, {"WORKSPACE_MCP_DISABLE_LOCAL_FILES": "true"})
        assert "ADVERTISED:False" in out
        text = out.split("RESULT:", 1)[1]
        assert "local file access is disabled" in text
        assert "'base64_content'" in text

    def test_http_without_opt_in_still_advertises_file_path(self):
        out = _run_subprocess(self.CODE, {})
        assert "ADVERTISED:True" in out
