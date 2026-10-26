import pytest

from auth.scopes import get_scopes_for_tools
from gartifacts.artifacts_guard import (
    ALLOW_WRITE_ENV,
    excess_scopes,
    serving_scope_problem,
)


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv(ALLOW_WRITE_ENV, raising=False)


def test_artifacts_only_instance_may_serve():
    assert serving_scope_problem(get_scopes_for_tools(["artifacts"])) == ""


@pytest.mark.parametrize("tools", [["drive"], ["gmail"], ["artifacts", "docs"]])
def test_instance_with_other_scopes_is_refused(tools):
    scopes = get_scopes_for_tools(tools)
    assert excess_scopes(scopes)
    problem = serving_scope_problem(scopes)
    assert "Drive read access" in problem and ALLOW_WRITE_ENV in problem


def test_override_allows_it(monkeypatch):
    monkeypatch.setenv(ALLOW_WRITE_ENV, "true")
    assert serving_scope_problem(get_scopes_for_tools(["gmail"])) == ""
