"""A Drive signed download that fails mid-stream, read over a real socket.

Drive streams without a Content-Length, so the only signal a client gets of a
cut transfer is the missing terminating chunk. That holds only if the error
reaches uvicorn through the whole middleware stack, which is what this pins."""

import asyncio
import socket
from unittest.mock import Mock, patch

import httpx
import pytest
import pytest_asyncio
import uvicorn
from google.oauth2.credentials import Credentials

import core.signed_downloads as sd
import gdrive.drive_helpers as drive_helpers
from core.server import server, set_transport_mode

CHUNK = 65_536


class _Downloader:
    """MediaIoBaseDownload stand-in that fails after ``good`` chunks."""

    good = 1

    def __init__(self, fh, request, chunksize):
        self._fh, self._n = fh, 0

    def next_chunk(self):
        if self._n >= self.good:
            raise OSError("connection reset by Google")
        self._n += 1
        self._fh.write(b"x" * CHUNK)
        return "status", False


@pytest_asyncio.fixture
async def live(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "live-client-secret-material")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setattr(sd, "build", lambda *a, **k: Mock())
    monkeypatch.setattr(sd, "MediaIoBaseDownload", _Downloader)
    monkeypatch.setattr(drive_helpers, "_media_request", lambda *a: Mock())
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
            log_level="critical",
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


@pytest.mark.asyncio
async def test_drive_mid_stream_failure_is_a_broken_transfer(live):
    url = sd.mint_url(
        source="drive", user_email="u@example.com", ref={"fid": "F"}, ttl_seconds=300
    )
    received = 0
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.RemoteProtocolError):
            async with http.stream("GET", url) as response:
                assert response.status_code == 200
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
    assert received <= CHUNK
