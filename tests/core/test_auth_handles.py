"""Tests for short claim-check auth handles, the sign-in link builder, and
the /auth route.

The handle store runs against the in-process memory fallback (no shared
backend configured), which exercises the same code paths the encrypted
shared store uses — only the backing store differs. Mirrors
``test_download_handles.py``.
"""

import asyncio
import time

import pytest

from core import auth_handles as ah

# A representative (long) Google authorization URL.
_AUTH_URL = (
    "https://accounts.google.com/o/oauth2/auth?response_type=code"
    "&client_id=123456789-abcdefghijklmnopqrstuvwxyz012345.apps.googleusercontent.com"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A47012%2Foauth2callback"
    "&scope="
    + "%20".join(f"https://www.googleapis.com/auth/scope{i}" for i in range(12))
    + "&state="
    + "a" * 32
    + "&code_challenge="
    + "b" * 43
    + "&code_challenge_method=S256&access_type=offline&prompt=consent"
)


@pytest.fixture(autouse=True)
def _reset_store(monkeypatch):
    from core import storage

    monkeypatch.delenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(storage, "_configured", None)
    monkeypatch.setattr(storage, "_configured_built", False)
    monkeypatch.setattr(ah, "_store", None)
    monkeypatch.setattr(ah, "_store_built", False)
    yield
    monkeypatch.setattr(ah, "_store", None)
    monkeypatch.setattr(ah, "_store_built", False)


class TestAuthHandleStore:
    @pytest.mark.asyncio
    async def test_round_trip(self):
        handle = await ah.store_auth_url_ref(_AUTH_URL, ttl_seconds=600)
        assert handle is not None
        # 16 random bytes → 22-char urlsafe handle
        assert len(handle) == 22
        assert ah._HANDLE_RE.fullmatch(handle)

        assert await ah.load_auth_url_ref(handle) == _AUTH_URL

    @pytest.mark.asyncio
    async def test_handles_are_unique(self):
        h1 = await ah.store_auth_url_ref(_AUTH_URL, ttl_seconds=600)
        h2 = await ah.store_auth_url_ref(_AUTH_URL, ttl_seconds=600)
        assert h1 != h2

    @pytest.mark.asyncio
    async def test_unknown_handle_returns_none(self):
        assert await ah.load_auth_url_ref("A" * 22) is None

    @pytest.mark.asyncio
    async def test_malformed_handles_rejected(self):
        for bad in ("", "short", "../../etc/passwd", "has space", "x" * 65, "a.b"):
            assert await ah.load_auth_url_ref(bad) is None

    @pytest.mark.asyncio
    async def test_store_ttl_expires_handle(self):
        handle = await ah.store_auth_url_ref(_AUTH_URL, ttl_seconds=0.05)
        await asyncio.sleep(0.1)
        assert await ah.load_auth_url_ref(handle) is None

    @pytest.mark.asyncio
    async def test_exp_is_backstop(self, monkeypatch):
        # Long store TTL but an already-past exp: the read-time backstop must reject.
        handle = await ah.store_auth_url_ref(_AUTH_URL, ttl_seconds=600)
        store = ah._build_store()
        poisoned = {"auth_url": _AUTH_URL, "exp": time.time() - 10}
        await store.put(handle, poisoned, collection=ah._COLLECTION, ttl=600)
        assert await ah.load_auth_url_ref(handle) is None

    @pytest.mark.asyncio
    async def test_missing_or_malformed_exp_fails_closed(self):
        handle = await ah.store_auth_url_ref(_AUTH_URL, ttl_seconds=600)
        store = ah._build_store()
        await store.put(
            handle, {"auth_url": _AUTH_URL}, collection=ah._COLLECTION, ttl=600
        )
        assert await ah.load_auth_url_ref(handle) is None
        await store.put(
            handle,
            {"auth_url": _AUTH_URL, "exp": "nope"},
            collection=ah._COLLECTION,
            ttl=600,
        )
        assert await ah.load_auth_url_ref(handle) is None

    @pytest.mark.asyncio
    async def test_non_google_url_refused_on_store(self):
        for bad in (
            "https://evil.example.com/phish",
            "http://accounts.google.com/o/oauth2/auth",  # not https
            "https://accounts.google.com.evil.com/o/oauth2/auth",
            "javascript:alert(1)",
            "not a url",
        ):
            assert await ah.store_auth_url_ref(bad, ttl_seconds=600) is None

    @pytest.mark.asyncio
    async def test_poisoned_non_google_url_rejected_on_load(self):
        # Defense-in-depth: even if a non-Google URL reached the store somehow,
        # load must refuse to hand it back (so /auth never open-redirects).
        handle = await ah.store_auth_url_ref(_AUTH_URL, ttl_seconds=600)
        store = ah._build_store()
        await store.put(
            handle,
            {"auth_url": "https://evil.example.com", "exp": time.time() + 600},
            collection=ah._COLLECTION,
            ttl=600,
        )
        assert await ah.load_auth_url_ref(handle) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_store_unavailable(self, monkeypatch):
        monkeypatch.setattr(ah, "_store", None)
        monkeypatch.setattr(ah, "_store_built", True)
        assert await ah.store_auth_url_ref(_AUTH_URL, ttl_seconds=600) is None
        assert await ah.load_auth_url_ref("A" * 22) is None


class TestBuildSignInLink:
    @pytest.mark.asyncio
    async def test_short_link_by_default(self, monkeypatch):
        from auth.google_auth import _build_sign_in_link

        link = await _build_sign_in_link(
            _AUTH_URL, "http://localhost:47012/oauth2callback"
        )
        assert link.startswith("http://localhost:47012/auth/")
        assert len(link) < 60
        # The handle resolves back to the full authorization URL.
        handle = link.rsplit("/", 1)[1]
        assert await ah.load_auth_url_ref(handle) == _AUTH_URL

    @pytest.mark.asyncio
    async def test_flag_forces_full_url(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_SHORT_AUTH_URLS", "false")
        from auth.google_auth import _build_sign_in_link

        link = await _build_sign_in_link(
            _AUTH_URL, "http://localhost:47012/oauth2callback"
        )
        assert link == _AUTH_URL

    @pytest.mark.asyncio
    async def test_falls_back_to_full_url_when_store_unavailable(self, monkeypatch):
        monkeypatch.setattr(ah, "_store", None)
        monkeypatch.setattr(ah, "_store_built", True)
        from auth.google_auth import _build_sign_in_link

        link = await _build_sign_in_link(
            _AUTH_URL, "http://localhost:47012/oauth2callback"
        )
        assert link == _AUTH_URL


class TestShortAuthRoute:
    def _request(self, handle: str):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/auth/{handle}",
            "raw_path": f"/auth/{handle}".encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("localhost", 8000),
            "path_params": {"handle": handle},
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        return Request(scope, receive)

    @pytest.mark.asyncio
    async def test_unknown_handle_403(self):
        from core.server import serve_short_auth_url

        response = await serve_short_auth_url(self._request("B" * 22))
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_valid_handle_302_redirects_to_google(self):
        from core.server import serve_short_auth_url

        handle = await ah.store_auth_url_ref(_AUTH_URL, ttl_seconds=600)
        response = await serve_short_auth_url(self._request(handle))
        assert response.status_code == 302
        assert response.headers["location"] == _AUTH_URL
