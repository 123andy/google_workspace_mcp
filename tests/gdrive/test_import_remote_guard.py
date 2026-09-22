"""Remote-transport guard for the import_to_google_* tools and update_drive_file.

'file_path' resolves on the machine the SERVER runs on. When the server is
remote (streamable-http) — e.g. a container with no view of the caller's
disk — a client-side path can never resolve, and before this guard it fell
through to validate_file_path() and failed with a bare "Path does not
exist", which reads as a typo rather than a topology mismatch.

The guard lives once in _import_with_conversion for the three import tools,
and once in update_drive_file; both build their message with the same helper.
validate_file_path() additionally names the server boundary in its own errors
as defense-in-depth for any unguarded path.
"""

import inspect
import os
import re
import subprocess
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError, validate_file_path  # noqa: E402
from gdrive.drive_tools import (  # noqa: E402
    import_to_google_doc,
    import_to_google_sheets,
    import_to_google_slides,
    update_drive_file,
)


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


REMOTE = patch("gdrive.drive_tools.get_transport_mode", return_value="streamable-http")
STDIO = patch("gdrive.drive_tools.get_transport_mode", return_value="stdio")


class TestRemoteFilePathRejected:
    """The import tools and update_drive_file reject file_path remotely, BEFORE
    touching the Drive API, with an error that names working alternatives."""

    @pytest.mark.asyncio
    @REMOTE
    async def test_doc_import_names_every_route_it_has(self, _mode):
        service = Mock()
        with pytest.raises(UserInputError) as exc:
            await _unwrap(import_to_google_doc)(
                service=service,
                user_google_email="user@example.com",
                file_name="Notes.md",
                file_path="/Users/someone/notes.md",
            )
        msg = str(exc.value)
        assert "streamable-http" in msg and "MCP server" in msg
        assert "'content'" in msg and "'file_url'" in msg
        assert "'base64_content'" in msg
        service.files.assert_not_called()

    @pytest.mark.asyncio
    @REMOTE
    async def test_slides_import_names_base64_content_but_not_content(self, _mode):
        """Slides has no content param — the error must not advertise one. It
        does take base64_content, which is its only inline route: telling a
        remote caller to go and host the file is wrong when they can send it."""
        service = Mock()
        with pytest.raises(UserInputError) as exc:
            await _unwrap(import_to_google_slides)(
                service=service,
                user_google_email="user@example.com",
                file_name="Deck",
                file_path="/Users/someone/deck.pptx",
            )
        msg = str(exc.value)
        assert "'file_url'" in msg and "'base64_content'" in msg
        assert "'content'" not in msg
        service.files.assert_not_called()

    @pytest.mark.asyncio
    @REMOTE
    async def test_update_drive_file_rejected_before_any_read(self, _mode):
        """update_drive_file reaches _resolve_import_media outside
        _import_with_conversion, so it needs its own guard: a cached-schema
        client sending file_path must be rejected before the server touches
        the Drive API or its own filesystem."""
        service = Mock()
        with pytest.raises(UserInputError) as exc:
            await _unwrap(update_drive_file)(
                service=service,
                user_google_email="user@example.com",
                file_id="abc123",
                file_path="/Users/someone/report.docx",
            )
        msg = str(exc.value)
        assert "'content'" in msg and "'file_url'" in msg
        # update_drive_file has no base64_content parameter.
        assert "'base64_content'" not in msg
        service.files.assert_not_called()

    @pytest.mark.asyncio
    @REMOTE
    async def test_sheets_import_rejected_too(self, _mode):
        service = Mock()
        with pytest.raises(UserInputError) as exc:
            await _unwrap(import_to_google_sheets)(
                service=service,
                user_google_email="user@example.com",
                file_name="Budget",
                file_path="/Users/someone/budget.xlsx",
            )
        msg = str(exc.value)
        assert "'content'" in msg and "'base64_content'" in msg
        assert "'file_url'" in msg
        service.files.assert_not_called()

    @pytest.mark.asyncio
    @REMOTE
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
        self, _mode, tool, kwargs
    ):
        """The routes are read from each tool's real signature, not restated
        here, so if upstream adds or removes one of these three parameters on
        a tool, this test fails instead of leaving the guidance quietly wrong."""
        fn = _unwrap(tool)
        params = set(inspect.signature(fn).parameters)
        has = {"content", "base64_content", "file_url"} & params

        with pytest.raises(UserInputError) as exc:
            await fn(
                service=Mock(),
                user_google_email="user@example.com",
                file_path="/Users/someone/file.bin",
                **kwargs,
            )
        advice = str(exc.value).split("Instead,", 1)[1]
        assert set(re.findall(r"'(\w+)'", advice)) == has

    @pytest.mark.asyncio
    @REMOTE
    async def test_update_offers_content_for_text_formats_only(self, _mode):
        """update_drive_file has no base64_content, and its 'content' rejects
        binary formats with advice to use file_path. Unqualified, the guard
        would send a .docx caller round in a circle."""
        with pytest.raises(UserInputError) as exc:
            await _unwrap(update_drive_file)(
                service=Mock(),
                user_google_email="user@example.com",
                file_id="abc123",
                file_path="/Users/someone/report.docx",
            )
        assert "for text formats, send it inline via 'content'" in str(exc.value)

    @pytest.mark.asyncio
    @REMOTE
    async def test_update_guard_answers_before_the_mode_check(self, _mode):
        """The mode check's own advice names file_path, so the guard runs first."""
        with pytest.raises(UserInputError, match="unavailable in remote"):
            await _unwrap(update_drive_file)(
                service=Mock(),
                user_google_email="user@example.com",
                file_id="abc123",
                file_path="/Users/someone/notes.md",
                mode="append",
            )


class TestWorkingRoutesUntouched:
    @pytest.mark.asyncio
    @REMOTE
    @patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock)
    async def test_inline_content_still_works_remotely(self, mock_folder, _mode):
        """The guard is file_path-specific: inline content imports fine remotely."""
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
        body = service.files().create.call_args.kwargs["body"]
        assert body["mimeType"] == "application/vnd.google-apps.document"
        assert "Successfully imported" in result

    @pytest.mark.asyncio
    @STDIO
    @patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock)
    async def test_file_path_still_works_on_stdio(
        self, mock_folder, _mode, tmp_path, monkeypatch
    ):
        """A local (stdio) server keeps the file_path route end to end."""
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

        result = await _unwrap(import_to_google_doc)(
            service=service,
            user_google_email="user@example.com",
            file_name="notes.md",
            file_path=str(src),
        )

        service.files().create.assert_called_once()
        assert "Successfully imported" in result


class TestAttachmentAdviceIsTransportAware:
    """gmail's attachment-failure guidance must not give server-operator advice
    (ALLOWED_FILE_DIRS, move-the-file) to callers of a remote server."""

    @patch("gmail.gmail_tools.get_transport_mode", return_value="streamable-http")
    def test_remote_advice_points_at_content(self, _mode):
        from gmail.gmail_tools import _format_attachment_error

        msg = _format_attachment_error(
            "/Users/someone/file.pdf",
            None,
            ValueError("path is outside the SERVER's permitted directories (x)."),
        )
        assert "cannot see the caller's filesystem" in msg
        assert "'content'" in msg
        assert "ALLOWED_FILE_DIRS" not in msg

    @patch("gmail.gmail_tools.get_transport_mode", return_value="stdio")
    def test_stdio_advice_keeps_operator_guidance(self, _mode):
        from gmail.gmail_tools import _format_attachment_error

        msg = _format_attachment_error(
            "/run/media/file.pdf",
            None,
            ValueError("path is outside the SERVER's permitted directories (x)."),
        )
        assert "ALLOWED_FILE_DIRS" in msg


class TestValidateFilePathNamesTheBoundary:
    @patch("core.utils.get_transport_mode", return_value="streamable-http")
    def test_missing_path_error_names_the_server_when_remote(self, _mode):
        with pytest.raises(FileNotFoundError) as exc:
            validate_file_path("/definitely/not/here.md")
        msg = str(exc.value)
        assert "on the MCP server" in msg
        assert "caller's" in msg

    @patch("core.utils.get_transport_mode", return_value="stdio")
    def test_missing_path_error_stays_plain_on_stdio(self, _mode):
        with pytest.raises(FileNotFoundError) as exc:
            validate_file_path("/definitely/not/here.md")
        assert "on the MCP server" not in str(exc.value)


class TestRemoteDecorationBoots:
    """exclude_args is validated by FastMCP at DECORATION time, and only when
    it is non-None — i.e. only on a streamable-http server. The rest of this
    suite imports the module under the stdio default, so a stale exclude_args
    naming a parameter that doesn't exist would pass every in-process test and
    then crash the real remote server at boot. Import in a subprocess with the
    transport pre-set to catch that class of bug."""

    def test_tool_module_imports_under_streamable_http(self):
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        code = (
            "from core.server import set_transport_mode; "
            "set_transport_mode('streamable-http'); "
            "import gdrive.drive_tools"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"gdrive.drive_tools failed to import on streamable-http:\n{result.stderr}"
        )


class TestGuardReachableThroughFastMCP:
    """exclude_args only removes file_path from the ADVERTISED schema. FastMCP
    validates incoming calls against the function signature, and an
    exclude_args parameter is still in it, so a client holding an older schema
    can still send file_path and it reaches the function — the guard is what
    answers, and it is what keeps a hidden parameter from
    resolving on the server's disk. The other tests call the functions
    directly and cannot see this, so go through a real client, in a subprocess
    because the schema is fixed at import time by the transport."""

    def test_stale_client_gets_the_guidance_not_a_generic_error(self):
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        code = """
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
            assert 'file_path' not in schema['properties'], 'file_path advertised'
            result = await client.call_tool(
                'import_to_google_slides',
                {'file_name': 'Deck', 'file_path': '/Users/someone/deck.pptx',
                 'user_google_email': 'user@example.com'},
                raise_on_error=False,
            )
            print('RESULT:' + result.content[0].text)

asyncio.run(main())
"""
        # Modes that reshape the tool signature (OAuth 2.1 drops
        # user_google_email, which this call sends) must not leak in from the
        # developer's shell.
        inherited = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "MCP_ENABLE_OAUTH21",
                "EXTERNAL_OAUTH21_PROVIDER",
                "WORKSPACE_MCP_STATELESS_MODE",
                "MCP_SINGLE_USER_MODE",
            )
        }
        env = {
            **inherited,
            "GOOGLE_OAUTH_CLIENT_ID": "test-client-id",
            "GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret",
        }
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        text = result.stdout.split("RESULT:", 1)[1]
        assert "'file_path' is unavailable in remote" in text
        assert "'base64_content'" in text


class TestStdioOnlyArgsHelper:
    """stdio_only_args drives schema-level hiding of server-side path params."""

    @patch("core.utils.get_transport_mode", return_value="streamable-http")
    def test_remote_returns_the_names_to_exclude(self, _mode):
        from core.utils import stdio_only_args

        assert stdio_only_args("file_path") == ["file_path"]
        assert stdio_only_args("a", "b") == ["a", "b"]

    @patch("core.utils.get_transport_mode", return_value="stdio")
    def test_stdio_returns_none_so_params_stay_advertised(self, _mode):
        from core.utils import stdio_only_args

        assert stdio_only_args("file_path") is None
