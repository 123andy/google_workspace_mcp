"""A mid-body failure must reach the client as a failed transfer, not a clean 200.

Runs a real uvicorn server on a real socket: ASGI test clients hide this bug
because they re-raise app errors instead of closing a connection.
"""

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from auth.mcp_session_middleware import MCPSessionMiddleware

CHUNK = b"x" * 65536


async def _stream_then_fail(request):
    async def body():
        yield CHUNK
        raise RuntimeError("upstream failed after 64 KiB")

    return StreamingResponse(body(), media_type="application/octet-stream")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _simple_app():
    from starlette.middleware import Middleware

    return Starlette(
        routes=[
            Route("/mcp/stream-fail", _stream_then_fail),
            Route("/other/stream-fail", _stream_then_fail),
        ],
        middleware=[Middleware(MCPSessionMiddleware)],
    )


def _full_app():
    """The real app (all production middleware) plus the failing routes."""
    from core.server import server

    app = server.http_app()
    app.router.routes[:0] = [
        Route("/mcp/stream-fail", _stream_then_fail),
        Route("/other/stream-fail", _stream_then_fail),
    ]
    return app


@pytest.fixture(params=[_simple_app, _full_app], ids=["simple", "full"])
def live_server(request):
    app = request.param()
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.xfail(strict=True, reason="BaseHTTPMiddleware ends the body cleanly")
@pytest.mark.parametrize("path", ["/mcp/stream-fail", "/other/stream-fail"])
def test_mid_body_failure_aborts_the_transfer(live_server, path):
    received = 0
    with pytest.raises(httpx.RemoteProtocolError):
        with httpx.stream("GET", live_server + path) as response:
            assert response.status_code == 200
            for chunk in response.iter_bytes():
                received += len(chunk)
    assert received <= len(CHUNK)
