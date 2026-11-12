"""WORKSPACE_MCP_STATELESS_HTTP turns on sessionless streamable HTTP on its own."""

import pytest

from core.server import use_stateless_http


@pytest.mark.parametrize(
    "flag, stateless_mode, expected",
    [
        (None, False, False),
        ("true", False, True),
        ("TRUE", False, True),
        (" true ", False, True),
        ("false", False, False),
        ("1", False, False),
        (None, True, True),
        ("false", True, True),
    ],
)
def test_use_stateless_http(monkeypatch, flag, stateless_mode, expected):
    if flag is None:
        monkeypatch.delenv("WORKSPACE_MCP_STATELESS_HTTP", raising=False)
    else:
        monkeypatch.setenv("WORKSPACE_MCP_STATELESS_HTTP", flag)
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: stateless_mode)
    assert use_stateless_http() is expected
