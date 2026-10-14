"""Capability paths (``/dl/{handle}`` and friends) must not reach the uvicorn
access log: the last path segment is the credential."""

import asyncio
import logging
import socket

import httpx
import pytest
import uvicorn

from core.log_formatter import (
    RedactCapabilityPathsFilter,
    install_noisy_log_filters,
    redact_capability_path,
)
from core.server import server
import core.signed_downloads  # noqa: F401  (routes register on import of core.server)

SECRET = "SECRETHANDLE0123456789ab"


@pytest.mark.parametrize(
    "path, expected",
    [
        (f"/dl/{SECRET}", "/dl/<redacted>"),
        (f"/ul/{SECRET}", "/ul/<redacted>"),
        (f"/attachments/signed/{SECRET}", "/attachments/signed/<redacted>"),
        (f"/auth/{SECRET}", "/auth/<redacted>"),
        (f"/dl/{SECRET}?x=1", "/dl/<redacted>"),
        # a literal operator route is not a credential
        ("/auth/callback", "/auth/callback"),
        ("/auth/callback?code=abc&state=xyz", "/auth/callback?code=abc&state=xyz"),
        ("/auth", "/auth"),
        (f"/auth/{SECRET}?x=1", "/auth/<redacted>"),
        # uvicorn logs root_path + path
        (f"/mcp-gw/dl/{SECRET}", "/mcp-gw/dl/<redacted>"),
        (
            f"/api/v1/attachments/signed/{SECRET}",
            "/api/v1/attachments/signed/<redacted>",
        ),
        (f"/mcp-gw/auth/{SECRET}", "/mcp-gw/auth/<redacted>"),
        ("/mcp-gw/auth/callback", "/mcp-gw/auth/callback"),
        ("/mcp", "/mcp"),
        ("/attachments/some-uuid", "/attachments/some-uuid"),
        ("/readyz", "/readyz"),
    ],
)
def test_redact_capability_path(path, expected):
    assert redact_capability_path(path) == expected


def test_filter_keeps_status_and_other_args():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", f"/dl/{SECRET}", "1.1", 403),
        None,
    )
    assert RedactCapabilityPathsFilter().filter(record) is True
    assert record.getMessage() == '127.0.0.1:1 - "GET /dl/<redacted> HTTP/1.1" 403'


def test_filter_ignores_unexpected_record_shapes():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "hi", (), None
    )
    assert RedactCapabilityPathsFilter().filter(record) is True
    assert record.getMessage() == "hi"


def test_install_is_idempotent():
    install_noisy_log_filters()
    install_noisy_log_filters()
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, RedactCapabilityPathsFilter) for f in filters) == 1


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.mark.asyncio
async def test_real_uvicorn_access_log_never_contains_the_handle():
    install_noisy_log_filters()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(
        server.http_app(),
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=True,
        lifespan="off",
    )
    capture = _Capture()
    # Config() has just applied uvicorn's logging setup; attach after it.
    logging.getLogger("uvicorn.access").addHandler(capture)
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    try:
        for _ in range(100):
            if srv.started:
                break
            await asyncio.sleep(0.05)
        assert srv.started
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            responses = [
                await http.get(f"/dl/{SECRET}"),
                await http.get(f"/attachments/signed/{SECRET}"),
                await http.get(f"/auth/{SECRET}"),
                await http.put(f"/ul/{SECRET}", content=b"x"),
            ]
    finally:
        srv.should_exit = True
        await task
        logging.getLogger("uvicorn.access").removeHandler(capture)

    assert len(responses) == 4
    text = "\n".join(capture.lines)
    assert SECRET not in text
    assert '"GET /dl/<redacted> HTTP/1.1" 4' in text  # status survives redaction
    assert "/attachments/signed/<redacted>" in text
    assert "/auth/<redacted>" in text


@pytest.mark.asyncio
async def test_real_uvicorn_root_path_prefix_is_still_redacted():
    install_noisy_log_filters()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(
        server.http_app(),
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=True,
        lifespan="off",
        root_path="/mcp-gw",
    )
    capture = _Capture()
    logging.getLogger("uvicorn.access").addHandler(capture)
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    try:
        for _ in range(100):
            if srv.started:
                break
            await asyncio.sleep(0.05)
        assert srv.started
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            await http.get(f"/dl/{SECRET}")
            await http.get("/auth/callback")
    finally:
        srv.should_exit = True
        await task
        logging.getLogger("uvicorn.access").removeHandler(capture)

    text = "\n".join(capture.lines)
    assert SECRET not in text
    assert "/mcp-gw/dl/<redacted>" in text
    assert "/auth/callback" in text
