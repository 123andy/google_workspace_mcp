"""
Unit tests for the --exclude-tools CLI flag.

The flag is a per-tool-name BLOCKLIST that COMPOSES with the normal selectors
(--permissions, --tools, --tool-tier, default). It removes the named tools from
whatever set was otherwise selected, but — crucially, and unlike --only-tools —
it does NOT change requested OAuth scopes. The token keeps the scopes of the
remaining tools, so it can drop a tool whose scope is shared with tools you keep
(e.g. drop the Drive sharing tools while keeping create/edit, both of which need
the same drive.file scope).

It is exercised across two layers:

- core/tool_registry.py: set_excluded_tools() / get_excluded_tools() and the
  blocklist branch of filter_server_tools(), which removes the named tools from
  a live server without touching scopes.
- main.py: argparse wiring, the unknown-tool validation, the absence of any
  mutual-exclusivity guard against --permissions, and the set_excluded_tools()
  call that runs for every selection mode.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Keep these tests independent of a developer's local .env. Importing main loads
# .env, and OAuth 2.1 mode changes tool schemas at decoration time. Mirrors
# tests/test_only_tools.py and tests/test_main_permissions_tier.py.
os.environ["MCP_ENABLE_OAUTH21"] = "false"
os.environ["WORKSPACE_MCP_STATELESS_MODE"] = "false"

import main  # noqa: E402

from auth.scopes import (  # noqa: E402
    BASE_SCOPES,
    DRIVE_FILE_SCOPE,
    get_scopes_for_tools,
    set_explicit_scopes,
    set_enabled_tools as set_enabled_scope_tools,
    set_read_only,
)
import auth.permissions as permissions  # noqa: E402
import core.tool_registry as tool_registry  # noqa: E402


# Tool names that genuinely exist in core/tool_tiers.yaml. We assert their
# presence via get_all_tool_names() rather than hard-coding the full list so the
# tests don't break every time a tool is added or moved between tiers.
DRIVE_SHARE_TOOLS = ["manage_drive_access", "set_drive_file_permissions"]
DRIVE_KEEP_TOOL = "create_drive_file"


class FakeLocalProvider:
    """Minimal stand-in for FastMCP's local_provider.

    get_tool_components() reads ``_components`` (keys shaped ``tool:name@ver``)
    and tool removal calls ``remove_tool(name)``. This fake honors both so we can
    drive the real filter_server_tools() against a controlled tool set.
    """

    def __init__(self, tool_names):
        """Seed the fake component map from the given tool names."""
        self._components = {f"tool:{name}@1": _FakeTool(name) for name in tool_names}

    def remove_tool(self, name):
        """Remove a tool by name, mirroring ``local_provider.remove_tool``."""
        self._components.pop(f"tool:{name}@1", None)


class _FakeTool:
    def __init__(self, name):
        """A fake tool exposing the attributes ``filter_server_tools`` reads."""
        # filter_server_tools reads ._required_google_scopes off .fn; the
        # exclude-tools branch never inspects scopes, but other branches might,
        # so give every fake tool a harmless empty scope list.
        self.fn = self
        self.__name__ = name
        self._required_google_scopes = []


class FakeServer:
    def __init__(self, tool_names):
        """Wrap a FakeLocalProvider seeded with the given tool names."""
        self.local_provider = FakeLocalProvider(tool_names)

    def tool_names(self):
        """Return the set of tool names currently registered."""
        return {
            key.split(":", 1)[1].rsplit("@", 1)[0]
            for key in self.local_provider._components
        }


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset module-global selection state so tests don't leak into each other."""
    set_enabled_scope_tools(None)
    set_read_only(False)
    tool_registry.set_enabled_tools(None)
    tool_registry.set_excluded_tools(None)
    set_explicit_scopes(None)
    permissions.set_permissions(None)
    yield
    set_enabled_scope_tools(None)
    set_read_only(False)
    tool_registry.set_enabled_tools(None)
    tool_registry.set_excluded_tools(None)
    set_explicit_scopes(None)
    permissions.set_permissions(None)


class TestExcludedToolsRegistry:
    """core/tool_registry.py: the blocklist setter/getter and filter branch."""

    def test_set_and_get_round_trip(self):
        """set_excluded_tools()/get_excluded_tools() round-trip, including clearing."""
        assert tool_registry.get_excluded_tools() is None
        tool_registry.set_excluded_tools({"manage_drive_access"})
        assert tool_registry.get_excluded_tools() == {"manage_drive_access"}
        tool_registry.set_excluded_tools(None)
        assert tool_registry.get_excluded_tools() is None

    def test_filter_removes_only_excluded_tools(self):
        """The named tools are removed; everything else is kept."""
        server = FakeServer(DRIVE_SHARE_TOOLS + [DRIVE_KEEP_TOOL])
        tool_registry.set_excluded_tools(set(DRIVE_SHARE_TOOLS))

        tool_registry.filter_server_tools(server)

        remaining = server.tool_names()
        for tool in DRIVE_SHARE_TOOLS:
            assert tool not in remaining
        assert DRIVE_KEEP_TOOL in remaining

    def test_filter_is_noop_when_no_exclusion_active(self):
        """With no selectors active at all, filter_server_tools early-returns."""
        server = FakeServer(DRIVE_SHARE_TOOLS + [DRIVE_KEEP_TOOL])
        tool_registry.set_excluded_tools(None)

        tool_registry.filter_server_tools(server)

        assert server.tool_names() == set(DRIVE_SHARE_TOOLS + [DRIVE_KEEP_TOOL])

    def test_exclusion_does_not_early_return(self):
        """Exclusion alone (no enabled-tools / read-only / permissions) must still
        run the filter — i.e. the early-return guard accounts for it."""
        server = FakeServer(["manage_drive_access", DRIVE_KEEP_TOOL])
        tool_registry.set_excluded_tools({"manage_drive_access"})

        tool_registry.filter_server_tools(server)

        assert "manage_drive_access" not in server.tool_names()


class TestComposesWithPermissions:
    """--exclude-tools must be allowed alongside --permissions (not rejected)."""

    def test_not_mutually_exclusive_with_permissions(self, monkeypatch):
        """Combining --permissions and --exclude-tools must NOT sys.exit at the
        mutual-exclusivity guards. We let main.main() run far enough to clear the
        guards, then stop it before it starts a server by failing the import."""
        monkeypatch.setattr(main, "configure_safe_logging", lambda: None)

        # Sentinel raised from inside the selection chain, AFTER the guards.
        class _ReachedSelection(Exception):
            pass

        def _boom(arg):
            """Sentinel: prove the selection chain was reached past the guards."""
            raise _ReachedSelection()

        # parse_permissions_arg is the first thing the --permissions branch calls,
        # which is well past every mutual-exclusivity guard.
        monkeypatch.setattr(main, "resolve_callback_port_for_transport", lambda t: None)
        monkeypatch.setattr(main, "validate_streamable_http_auth", lambda t: None)
        import auth.permissions as perms_mod

        monkeypatch.setattr(perms_mod, "parse_permissions_arg", _boom)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "main.py",
                "--permissions",
                "drive:full",
                "--exclude-tools",
                "manage_drive_access",
            ],
        )

        # If the guard rejected the combo it would SystemExit(1) BEFORE reaching
        # parse_permissions_arg. Reaching our sentinel proves the combo is allowed.
        with pytest.raises(_ReachedSelection):
            main.main()


class TestUnknownToolValidation:
    def test_unknown_tool_name_exits(self, monkeypatch, capsys):
        """An unknown --exclude-tools name exits(1) with an 'unknown tool name' error."""
        monkeypatch.setattr(main, "configure_safe_logging", lambda: None)
        monkeypatch.setattr(main, "resolve_callback_port_for_transport", lambda t: None)
        monkeypatch.setattr(main, "validate_streamable_http_auth", lambda t: None)
        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "--exclude-tools", "not_a_real_tool"],
        )

        with pytest.raises(SystemExit) as exc:
            main.main()

        assert exc.value.code == 1
        assert "unknown tool name" in capsys.readouterr().err.lower()


class TestExclusionDoesNotAlterScopes:
    """The whole point: dropping a tool must NOT drop any OAuth scope."""

    def test_setting_excluded_tools_leaves_scopes_untouched(self):
        """Excluding the Drive sharing tools must not change the scope set that
        get_scopes_for_tools() derives for the Drive service. Both the kept and
        the excluded Drive tools share drive.file, so the scope must remain."""
        before = set(get_scopes_for_tools(["drive"]))
        assert DRIVE_FILE_SCOPE in before

        tool_registry.set_excluded_tools(set(DRIVE_SHARE_TOOLS))
        after = set(get_scopes_for_tools(["drive"]))

        assert after == before
        assert DRIVE_FILE_SCOPE in after

    def test_exclusion_does_not_set_explicit_scopes(self):
        """--exclude-tools must never engage the explicit-scope override that
        --only-tools uses; the normal scope path stays in effect."""
        tool_registry.set_excluded_tools(set(DRIVE_SHARE_TOOLS))
        scopes = set(get_scopes_for_tools(["drive"]))
        # Normal path returns base + service scopes (more than base alone),
        # confirming the explicit-only short-circuit is NOT active.
        assert scopes > set(BASE_SCOPES)
