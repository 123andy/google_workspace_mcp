"""Truncation behaviour of the Chat signed-download route over a real socket.

``httpx.ASGITransport`` hands the app's exception straight to the test, so it
cannot show what a client on the wire sees. These tests run the real app under
uvicorn and read the response with a real HTTP client."""

import asyncio
import socket
from unittest.mock import Mock, patch

import httpx
import pytest
import pytest_asyncio
import uvicorn
from google.oauth2.credentials import Credentials

import core.signed_downloads as sd
from core.server import server, set_transport_mode
import gchat.chat_tools  # noqa: F401  (the route serves a source only when its tools are loaded)

RealClient = httpx.AsyncClient
TOTAL = 140_000
SENT = 65_536


class _Cut(httpx.AsyncByteStream):
    def __init__(self, sent, error):
        self._sent, self._error = sent, error

    async def __aiter__(self):
        yield b"x" * self._sent
        if self._error:
            raise httpx.ReadError("connection reset")


@pytest_asyncio.fixture
async def live(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "live-client-secret-material")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_MAX_FILE_BYTES", raising=False)
    sd._signing_key.cache_clear()
    from core.config import get_transport_mode

    saved = get_transport_mode()
    set_transport_mode("streamable-http")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", f"http://127.0.0.1:{port}")
    srv = uvicorn.Server(
        uvicorn.Config(
            server.http_app(),
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="off",
        )
    )
    creds = Credentials(token="t", refresh_token="r")
    with patch.object(sd, "_recover_credentials", Mock(return_value=creds)):
        task = asyncio.create_task(srv.serve())
        try:
            for _ in range(100):
                if srv.started:
                    break
                await asyncio.sleep(0.05)
            assert srv.started
            yield port
        finally:
            srv.should_exit = True
            await task
            set_transport_mode(saved)
            sd._signing_key.cache_clear()


def _upstream(monkeypatch, handler):
    monkeypatch.setattr(
        sd.httpx,
        "AsyncClient",
        lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw),
    )


def _link(mime_type=None):
    return sd.mint_url(
        source="chat",
        user_email="u@example.com",
        ref={"rn": "spaces/S/attachments/A"},
        ttl_seconds=300,
        mime_type=mime_type,
    )


async def _fetch(port, mime_type=None):
    """Return (status, bytes received); raises if the client sees a broken transfer."""
    response = await _get(port, mime_type)
    return response.status_code, response.content


async def _get(port, mime_type=None):
    path = _link(mime_type).split(f"127.0.0.1:{port}")[1]
    async with RealClient(base_url=f"http://127.0.0.1:{port}") as http:
        return await http.get(path)


@pytest.mark.asyncio
async def test_declared_length_cut_is_visible_to_the_client(live, monkeypatch):
    _upstream(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-length": str(TOTAL)},
            stream=_Cut(SENT, error=True),
        ),
    )
    with pytest.raises(httpx.HTTPError):
        await _fetch(live)


@pytest.mark.asyncio
async def test_declared_length_clean_early_end_is_visible_to_the_client(
    live, monkeypatch
):
    _upstream(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-length": str(TOTAL)},
            stream=_Cut(SENT, error=False),
        ),
    )
    with pytest.raises(httpx.HTTPError):
        await _fetch(live)


@pytest.mark.asyncio
async def test_complete_download_has_content_length_and_all_bytes(live, monkeypatch):
    _upstream(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-length": str(SENT)},
            stream=_Cut(SENT, error=False),
        ),
    )
    status, body = await _fetch(live)
    assert (status, len(body)) == (200, SENT)


@pytest.mark.asyncio
async def test_no_declared_length_is_refused_with_a_generic_502(live, monkeypatch):
    _upstream(
        monkeypatch,
        lambda request: httpx.Response(200, stream=_Cut(SENT, error=False)),
    )
    status, body = await _fetch(live)
    assert status == 502
    assert b"Content-Length" not in body and b"x" * 10 not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", [" text/plain", "application/pdf "])
async def test_type_with_edge_whitespace_still_downloads(live, monkeypatch, mime_type):
    """uvicorn's h11 refuses a header value with spaces at its ends, so such a
    type reaching the response would drop the connection after the 200."""
    _upstream(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-length": str(SENT)},
            stream=_Cut(SENT, error=False),
        ),
    )
    response = await _get(live, mime_type)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert len(response.content) == SENT
