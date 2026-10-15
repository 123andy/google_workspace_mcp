"""``/ul/{handle}`` over real sockets: the real app under uvicorn, a real HTTP
client, and a fake Google upload endpoint on localhost.

The fake Google is a raw asyncio server so a test can cut its connection
mid-body or notice its peer vanish, which an ASGI fake cannot. The session-URL
check is pointed at it by patching ``upload_relay._allowed_origin``, the test
hook that no setting can reach."""

import asyncio
import hashlib
import json
import socket
import tracemalloc
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio
import uvicorn
from google.oauth2.credentials import Credentials
from key_value.aio.stores.memory import MemoryStore

import core.server as core_server
import core.signed_downloads as sd
import core.upload_relay as ur
from core.server import server, set_transport_mode

USER = "user@example.com"
MIB = 1024 * 1024


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeGoogle:
    """Minimal HTTP/1.1 upload endpoint: hashes the body, or misbehaves on cue."""

    def __init__(self):
        self.mode = "ok"
        self.drop_after = 0
        self.peer_vanished = asyncio.Event()
        self.received = 0

    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = {}
            for line in head.decode("latin-1").split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            length = int(headers["content-length"])
            digest = hashlib.sha256()
            self.received = 0
            while self.received < length:
                chunk = await reader.read(min(256 * 1024, length - self.received))
                if not chunk:
                    self.peer_vanished.set()
                    return
                digest.update(chunk)
                self.received += len(chunk)
                if self.mode == "drop" and self.received >= self.drop_after:
                    writer.transport.abort()
                    return
            if self.mode == "hang":
                await asyncio.sleep(30)
                return
            body = json.dumps(
                {"id": "FILE1", "sha256": digest.hexdigest(), "size": self.received}
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            self.peer_vanished.set()
        finally:
            writer.close()


@pytest_asyncio.fixture
async def live(monkeypatch):
    google = FakeGoogle()
    gserver = await asyncio.start_server(google.handle, "127.0.0.1", 0)
    gport = gserver.sockets[0].getsockname()[1]
    monkeypatch.setattr(ur, "_allowed_origin", lambda: ("http", "127.0.0.1", gport))

    monkeypatch.setenv(sd.FLAG_ENV, "true")
    monkeypatch.setenv(ur.FLAG_ENV, "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "live-relay-secret")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.delenv(ur.MAX_BYTES_ENV, raising=False)
    sd._signing_key.cache_clear()
    monkeypatch.setattr(ur, "_store", MemoryStore())
    monkeypatch.setattr(ur, "_store_built", True)
    monkeypatch.setattr(ur, "_in_flight", set())
    monkeypatch.setattr(
        sd, "_recover_credentials", Mock(return_value=Credentials("t", "r"))
    )
    from core.config import get_transport_mode

    saved = get_transport_mode()
    set_transport_mode("streamable-http")
    before = list(server._additional_http_routes)
    core_server._upload_relay_route_registered = False
    core_server._ensure_upload_relay_route()

    port = _port()
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
    task = asyncio.create_task(srv.serve())
    try:
        for _ in range(100):
            if srv.started:
                break
            await asyncio.sleep(0.05)
        assert srv.started
        session = (
            f"http://127.0.0.1:{gport}/upload/drive/v3/files"
            "?uploadType=resumable&upload_id=live"
        )
        handle = await ur.store_session(session, USER, "application/octet-stream")
        assert handle
        yield google, f"http://127.0.0.1:{port}/ul/{handle}", handle, port
    finally:
        srv.should_exit = True
        await task
        gserver.close()
        await gserver.wait_closed()
        server._additional_http_routes[:] = before
        core_server._upload_relay_route_registered = False
        set_transport_mode(saved)
        sd._signing_key.cache_clear()


def _payload(total: int, chunk: int = MIB):
    """Deterministic bytes, produced a chunk at a time, plus their digest."""
    digest = hashlib.sha256()
    block = bytes(range(256)) * (chunk // 256)

    async def gen():
        sent = 0
        n = 0
        while sent < total:
            piece = block[: min(chunk, total - sent)]
            # Vary each chunk so a reordered or repeated chunk changes the hash.
            piece = n.to_bytes(4, "big") + piece[4:]
            digest.update(piece)
            sent += len(piece)
            n += 1
            yield piece

    return gen(), digest


@pytest.mark.asyncio
async def test_50_mib_upload_arrives_intact_without_being_buffered(live):
    google, url, handle, _ = live
    total = 50 * MIB
    body, digest = _payload(total)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        async with httpx.AsyncClient(timeout=120) as http:
            response = await http.put(
                url, content=body, headers={"Content-Length": str(total)}
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["size"] == total
    assert reply["sha256"] == digest.hexdigest()
    # Client, relay and fake Google share this process; whole-body buffering
    # anywhere would put the peak near 50 MiB.
    assert peak < 20 * MIB, f"peak {peak / MIB:.1f} MiB"
    assert (await ur._load(handle))["state"] == "done"


@pytest.mark.asyncio
async def test_google_cut_mid_upload_is_a_visible_failure(live):
    google, url, handle, _ = live
    google.mode, google.drop_after = "drop", 4 * MIB
    total = 20 * MIB
    body, _ = _payload(total)
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.put(
                url, content=body, headers={"Content-Length": str(total)}
            )
    except httpx.HTTPError:
        status = None  # the relay closed on us mid-send: also a failure
    else:
        status = response.status_code
        assert status == 502, response.text
    assert status in (None, 502)
    record = await ur._load(handle)
    assert record["state"] == "active"  # not done, and not closed: resumable


@pytest.mark.asyncio
async def test_client_vanishing_mid_upload_closes_the_google_request(live):
    google, url, handle, port = live
    google.mode = "watch"
    path = url.split(f":{port}", 1)[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"PUT {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Content-Length: {10 * MIB}\r\n\r\n".encode()
    )
    writer.write(b"z" * MIB)
    await writer.drain()
    for _ in range(100):
        if google.received >= 512 * 1024:
            break
        await asyncio.sleep(0.02)
    writer.transport.abort()

    await asyncio.wait_for(google.peer_vanished.wait(), 10)
    for _ in range(100):
        if not ur._in_flight:
            break
        await asyncio.sleep(0.05)
    assert not ur._in_flight
    record = await ur._load(handle)
    assert record["state"] == "active" and record["reqs"] == 1
    assert 0 < record["bytes"] < 10 * MIB


@pytest.mark.asyncio
async def test_google_never_answering_is_504(live, monkeypatch):
    google, url, handle, _ = live
    google.mode = "hang"
    monkeypatch.setattr(
        ur, "_UPSTREAM_TIMEOUT", httpx.Timeout(connect=5, read=1, write=5, pool=5)
    )
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.put(url, content=b"x" * 1000)
    assert response.status_code == 504
    assert (await ur._load(handle))["state"] == "active"
