"""Short claim-check download links (``/dl/{handle}``) over the signed download
URLs of ``core.signed_downloads`` (this fork; see ``core.download_handles``).

The contract under test:

- a tool call mints ``/dl/{handle}`` by default, and a GET on it is served by
  exactly the long form's ``serve`` — same bytes, headers and status codes;
- an unknown, malformed or expired handle answers exactly as a bad token does;
- the handle reveals nothing: random, unrelated to the token, and the record
  is encrypted at rest on a shared backend;
- a handle store failure falls back to the long link, never a dead link;
- ``WORKSPACE_MCP_SHORT_SIGNED_URLS=false`` keeps the long form.
"""

import base64
import json
import re
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastmcp import Client
from key_value.aio.stores.memory import MemoryStore

import auth.service_decorator as service_decorator
import core.download_handles as dh
import core.signed_downloads as sd
from core.server import server, set_transport_mode
from core.storage import ConfiguredKvStore
import gmail.gmail_tools  # noqa: F401  (registers the tool under test)

PAYLOAD = b"%PDF-1.4 short link round trip"
LONG_PREFIX = "http://testserver/attachments/signed/"
SHORT_PREFIX = "http://testserver/dl/"
HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")


@pytest.fixture(autouse=True)
def fresh_handle_store(monkeypatch):
    """Each test gets its own in-process store and the fork's default (on)."""
    monkeypatch.setattr(dh, "_store", None)
    monkeypatch.setattr(dh, "_store_built", False)
    monkeypatch.delenv(dh.FLAG_ENV, raising=False)


@pytest.fixture
def signed_env(monkeypatch, tmp_path):
    """Signed downloads on over streamable-http with a derivable key."""
    monkeypatch.setattr("core.attachment_storage.STORAGE_DIR", tmp_path)  # never $HOME
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "e2e-client-secret-material")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "http://testserver")
    sd._signing_key.cache_clear()
    from core.config import get_transport_mode

    saved_mode = get_transport_mode()
    set_transport_mode("streamable-http")
    try:
        yield
    finally:
        set_transport_mode(saved_mode)
        sd._signing_key.cache_clear()


def _gmail_service():
    service = Mock()
    service.users().messages().attachments().get().execute.return_value = {
        "size": len(PAYLOAD),
        "data": base64.urlsafe_b64encode(PAYLOAD).decode(),
    }
    service.users().messages().get().execute.return_value = {
        "payload": {
            "parts": [
                {
                    "filename": "invoice.pdf",
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": "att-1", "size": len(PAYLOAD)},
                }
            ]
        }
    }
    return service


def _owner_patches(service):
    creds = Mock(valid=True, expiry=datetime.utcnow() + timedelta(hours=1))
    store = Mock()
    store.get_credentials = lambda email: creds if email == "user@example.com" else None
    return (
        patch.object(
            service_decorator,
            "_authenticate_service",
            AsyncMock(return_value=(service, "user@example.com")),
        ),
        patch("auth.oauth21_session_store.get_oauth21_session_store", lambda: store),
        patch.object(sd, "build", lambda *a, **k: service),
    )


async def _mint_via_tool(client: Client) -> str:
    result = await client.call_tool(
        "get_gmail_attachment_content",
        {
            "message_id": "msg-1",
            "attachment_id": "att-1",
            "user_google_email": "user@example.com",
        },
    )
    text = result.content[0].text
    assert "streamed on demand" in text, text
    return next(
        line.split("Download URL: ", 1)[1]
        for line in text.splitlines()
        if "Download URL:" in line
    )


def _http():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.http_app()),
        base_url="http://testserver",
    )


def _mint_token() -> str:
    url = sd.mint_url(
        source="gmail",
        user_email="user@example.com",
        ref={"mid": "msg-1", "aid": "att-1"},
        ttl_seconds=600,
        filename="invoice.pdf",
    )
    return dh.split_signed_url(url)[1]


# --- Round trip ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_mints_short_link_and_route_serves_it_like_the_long_form(
    signed_env,
):
    service = _gmail_service()
    a, b, c = _owner_patches(service)
    with a, b, c:
        async with Client(server) as client:
            short_url = await _mint_via_tool(client)
        assert short_url.startswith(SHORT_PREFIX), short_url
        handle = short_url[len(SHORT_PREFIX) :]
        assert HANDLE_RE.fullmatch(handle), handle

        # The stored token is the long form's token: serving both must be identical.
        token = await dh.load_download_ref(handle)
        assert token
        async with _http() as http:
            short = await http.get(short_url)
            long = await http.get(LONG_PREFIX + token)

    assert short.status_code == 200
    assert short.content == PAYLOAD
    assert short.headers["content-type"].startswith("application/pdf")
    assert 'filename="invoice.pdf"' in short.headers["content-disposition"]
    assert short.headers["x-content-type-options"] == "nosniff"
    assert short.headers["cache-control"] == "no-store"
    # Not a redirect, and byte-for-byte what the long form serves.
    assert short.status_code == long.status_code
    assert short.content == long.content
    assert dict(short.headers) == dict(long.headers)


@pytest.mark.asyncio
async def test_short_route_never_redirects_to_the_long_url(signed_env):
    token = _mint_token()
    handle = await dh.store_download_ref(token, 600)
    a, b, c = _owner_patches(_gmail_service())
    with a, b, c:
        async with _http() as http:
            response = await http.get(SHORT_PREFIX + handle)
    assert response.status_code == 200
    assert "location" not in response.headers
    assert token not in response.text


# --- Bad handles answer exactly like bad tokens ------------------------------------


@pytest.mark.asyncio
async def test_unknown_or_malformed_handle_matches_a_bad_token(signed_env):
    async with _http() as http:
        bad_token = await http.get(LONG_PREFIX + "not-a-token")
        unknown = await http.get(SHORT_PREFIX + "AAAAAAAAAAAAAAAAAAAAAA")
        too_short = await http.get(SHORT_PREFIX + "abc")
        odd_chars = await http.get(SHORT_PREFIX + "AAAAAAAAAAAAAAAAAAAA.=")
        # A path segment the router refuses ("/dl/../etc" after decoding) never
        # reaches either route; both answer with the router's own 404.
        garbage_long = await http.get(LONG_PREFIX + "%2e%2e%2fetc")
        garbage_short = await http.get(SHORT_PREFIX + "%2e%2e%2fetc")
    assert bad_token.status_code == 403
    for response in (unknown, too_short, odd_chars):
        assert response.status_code == bad_token.status_code
        assert response.content == bad_token.content
        assert dict(response.headers) == dict(bad_token.headers)
    assert garbage_short.status_code == garbage_long.status_code == 404


@pytest.mark.asyncio
async def test_feature_off_short_route_is_inert_like_the_long_one(monkeypatch):
    """With signed downloads off the long route answers 404; so does /dl."""
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", "false")
    async with _http() as http:
        long = await http.get(LONG_PREFIX + "anything")
        short = await http.get(SHORT_PREFIX + "AAAAAAAAAAAAAAAAAAAAAA")
    assert long.status_code == 404
    assert short.status_code == 404
    assert short.content == long.content


@pytest.mark.asyncio
async def test_expired_handle_is_refused_like_a_bad_token(signed_env, monkeypatch):
    token = _mint_token()
    handle = await dh.store_download_ref(token, 60)
    assert await dh.load_download_ref(handle) == token

    # The read-time backstop: the record's exp has passed even if the backend's
    # own TTL had not fired.
    real_time = dh.time.time
    monkeypatch.setattr(dh.time, "time", lambda: real_time() + 120)
    assert await dh.load_download_ref(handle) is None

    async with _http() as http:
        bad_token = await http.get(LONG_PREFIX + "not-a-token")
        expired = await http.get(SHORT_PREFIX + handle)
    assert expired.status_code == bad_token.status_code == 403
    assert expired.content == bad_token.content


@pytest.mark.asyncio
async def test_store_ttl_equals_the_tokens_remaining_life():
    calls = []

    class Recording(MemoryStore):
        async def put(self, key, value, *, collection=None, ttl=None):
            calls.append(ttl)
            return await super().put(key, value, collection=collection, ttl=ttl)

    with patch.object(dh, "_build_store", lambda: Recording()):
        offer = await dh.shorten_signed_url((LONG_PREFIX + "tok", 540))
    assert offer[0].startswith(SHORT_PREFIX) and offer[1] == 540
    assert calls == [540]
    assert await dh.store_download_ref("tok", 0) is None
    assert await dh.store_download_ref("", 60) is None


# --- The handle reveals nothing ------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_reveals_nothing_about_the_token_or_its_claims(signed_env):
    token = _mint_token()
    first = await dh.store_download_ref(token, 600)
    second = await dh.store_download_ref(token, 600)
    for handle in (first, second):
        assert HANDLE_RE.fullmatch(handle)
        assert handle not in token and token not in handle
        for secret in ("user@example.com", "msg-1", "att-1", "invoice"):
            assert secret not in handle
    assert first != second  # random per mint, not derived from the token


@pytest.mark.asyncio
async def test_records_are_encrypted_at_rest_on_a_shared_backend(monkeypatch):
    """Through the KV builder: a configured (Postgres/Valkey-shaped) backend gets
    the Fernet wrapper under the handle-specific key, so the raw row never holds
    the token in the clear. The in-memory backend is what the tests above use."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shared-backend-secret-material")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    raw = MemoryStore()
    configured = ConfiguredKvStore(
        store=raw, backend="postgres", detail="test", needs_encryption=True
    )
    with patch("core.storage.get_configured_kv_store", lambda: configured):
        token = "gAAAAA-not-really-a-token-but-must-not-appear-in-the-clear"
        handle = await dh.store_download_ref(token, 600)
        assert await dh.load_download_ref(handle) == token

    row = await raw.get(handle, collection=dh._COLLECTION)
    assert row is not None
    assert token not in json.dumps(row)


# --- Fallbacks -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_failure_falls_back_to_the_long_link():
    offer = (LONG_PREFIX + "tok", 600)

    class Broken(MemoryStore):
        async def put(self, *a, **k):
            raise RuntimeError("backend down")

    with patch.object(dh, "_build_store", lambda: Broken()):
        assert await dh.shorten_signed_url(offer) == offer
    with patch.object(dh, "_build_store", lambda: None):
        assert await dh.shorten_signed_url(offer) == offer


@pytest.mark.asyncio
async def test_load_failure_is_an_unknown_handle():
    class Broken(MemoryStore):
        async def get(self, *a, **k):
            raise RuntimeError("backend down")

    with patch.object(dh, "_build_store", lambda: Broken()):
        assert await dh.load_download_ref("AAAAAAAAAAAAAAAAAAAAAA") is None


@pytest.mark.asyncio
async def test_feature_off_keeps_long_links(monkeypatch, signed_env):
    monkeypatch.setenv(dh.FLAG_ENV, "false")
    offer = (LONG_PREFIX + "tok", 600)
    assert await dh.shorten_signed_url(offer) == offer

    service = _gmail_service()
    a, b, c = _owner_patches(service)
    with a, b, c:
        async with Client(server) as client:
            url = await _mint_via_tool(client)
    assert url.startswith(LONG_PREFIX), url


@pytest.mark.asyncio
async def test_unrecognised_offers_pass_through():
    assert await dh.shorten_signed_url(None) is None
    for url in (
        "https://elsewhere.example/file.pdf",
        "http://testserver/attachments/signed/",
        "http://testserver/attachments/signed/tok/extra",
    ):
        assert await dh.shorten_signed_url((url, 600)) == (url, 600)
