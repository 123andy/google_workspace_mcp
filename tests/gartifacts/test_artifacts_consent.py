from types import SimpleNamespace

import pytest

from auth.scopes import DRIVE_READONLY_SCOPE, DRIVE_SCOPE
from core import server as core_server
from gartifacts import artifacts_consent as consent


@pytest.fixture(autouse=True)
def clean():
    consent._pending.clear()


@pytest.mark.parametrize(
    "path,ok",
    [
        ("/a/k7f3q9xm-x/", True),
        ("/a/k7f3q9xm-x/page.html?x=1", True),
        ("//evil.example.com/a/", False),
        ("https://evil.example.com/a/", False),
        ("/a/../x", True),
        ("/other", False),
        ("/a/x\r\nSet-Cookie: y=1", False),
        ("/a/x\\y", False),
        ("", False),
    ],
)
def test_safe_return_paths(path, ok):
    assert consent.is_safe_return_path(path) is ok


def test_return_is_remembered_once():
    consent.remember_return("state1", "/a/k7f3q9xm-x/", "u@scientist.com")
    assert consent.pop_return("state1") == ("/a/k7f3q9xm-x/", "u@scientist.com")
    assert consent.pop_return("state1") is None
    assert consent.pop_return(None) is None
    assert consent.pop_return("unknown") is None


def test_return_expires(monkeypatch):
    consent.remember_return("state1", "/a/k7f3q9xm-x/", "u@scientist.com")
    path, email, _ = consent._pending["state1"]
    consent._pending["state1"] = (path, email, 0.0)
    assert consent.pop_return("state1") is None


def test_pending_is_bounded(monkeypatch):
    monkeypatch.setattr(consent, "_MAX_PENDING", 3)
    for n in range(10):
        consent.remember_return(f"s{n}", "/a/x/", "u@scientist.com")
    assert len(consent._pending) <= 3


def test_begin_consent_binds_the_account(monkeypatch):
    stored = {}

    class Flow:
        code_verifier = "verifier"

        def authorization_url(self, **kwargs):
            stored["auth_kwargs"] = kwargs
            return "https://accounts.google.com/o/oauth2/auth?x=1", "s"

    class Store:
        def store_oauth_state(self, state, **kwargs):
            stored["state"] = state
            stored["kwargs"] = kwargs

    monkeypatch.setattr(consent, "create_oauth_flow", lambda **kw: Flow())
    monkeypatch.setattr(consent, "get_oauth21_session_store", lambda: Store())
    monkeypatch.setattr(consent, "get_oauth_redirect_uri", lambda: "https://a/cb")
    monkeypatch.setattr(consent, "get_current_scopes", lambda: ["s"])
    url = consent.begin_consent("u@scientist.com", "/a/k7f3q9xm-x/")
    assert url.startswith("https://accounts.google.com/")
    assert stored["kwargs"]["expected_user_email"] == "u@scientist.com"
    assert stored["kwargs"]["enforce_user_email_match"] is True
    assert stored["kwargs"]["principal_source"] == "gateway_assertion"
    assert stored["auth_kwargs"]["prompt"] == "consent"
    assert consent.pop_return(stored["state"]) == ("/a/k7f3q9xm-x/", "u@scientist.com")
    with pytest.raises(ValueError):
        consent.begin_consent("u@scientist.com", "https://evil.example.com/")


def test_finish_redirects_back_when_drive_read_was_granted():
    consent.remember_return("s1", "/a/k7f3q9xm-x/p%20q.html", "u@scientist.com")
    creds = SimpleNamespace(scopes=[DRIVE_READONLY_SCOPE, "openid"])
    response = consent.finish_viewer_consent("s1", creds)
    assert response.status_code == 302
    assert response.headers["location"] == "/a/k7f3q9xm-x/p%20q.html"


def test_finish_accepts_full_drive_scope():
    consent.remember_return("s1", "/a/x/", "u@scientist.com")
    assert (
        consent.finish_viewer_consent(
            "s1", SimpleNamespace(scopes=[DRIVE_SCOPE])
        ).status_code
        == 302
    )


def test_finish_refuses_a_partial_grant():
    consent.remember_return("s1", "/a/x/", "u@scientist.com")
    response = consent.finish_viewer_consent("s1", SimpleNamespace(scopes=["openid"]))
    assert response.status_code == 403


def test_finish_ignores_other_states():
    assert consent.finish_viewer_consent("unknown", SimpleNamespace(scopes=[])) is None
    assert consent.finish_viewer_consent(None, SimpleNamespace(scopes=[])) is None


class TestOriginExemption:
    @pytest.fixture(autouse=True)
    def viewer_on(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_ARTIFACTS_VIEWER", "true")

    def scope(self, method="GET", path="/c/tok/index.html"):
        return {"method": method, "path": path}

    def test_allows_null_origin_reads_of_content(self):
        assert core_server._is_artifact_content_origin_allowed(self.scope(), "null")
        assert core_server._is_artifact_content_origin_allowed(
            self.scope("HEAD"), "null"
        )

    def test_allows_null_origin_report_posts(self):
        scope = self.scope("POST", "/csp-report")
        assert core_server._is_artifact_content_origin_allowed(scope, "null")

    @pytest.mark.parametrize(
        "method,path,origin",
        [
            ("POST", "/c/tok/", "null"),
            ("POST", "/csp-report/x", "null"),
            ("GET", "/csp-report", "https://evil.example.com"),
            ("POST", "/csp-report", "https://evil.example.com"),
            ("GET", "/mcp", "null"),
            ("GET", "/a/x/", "null"),
            ("GET", "/dl/abc", "null"),
            ("GET", "/c/tok/", "https://evil.example.com"),
        ],
    )
    def test_nothing_else_is_exempt(self, method, path, origin):
        scope = self.scope(method, path)
        assert not core_server._is_artifact_content_origin_allowed(scope, origin)

    def test_off_without_the_switch(self, monkeypatch):
        monkeypatch.delenv("WORKSPACE_MCP_ARTIFACTS_VIEWER")
        assert not core_server._is_artifact_content_origin_allowed(self.scope(), "null")
