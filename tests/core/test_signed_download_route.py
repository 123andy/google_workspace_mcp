"""End to end through the real server: a tool call over FastMCP's in-memory client
mints a signed URL, and an HTTP GET through the ASGI app streams the bytes. A
tampered token is refused at the route.
"""

import base64
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastmcp import Client

import auth.service_decorator as service_decorator
import core.signed_downloads as sd
from core.server import server, set_transport_mode
import gmail.gmail_tools  # noqa: F401  (registers the tool under test)

PAYLOAD = b"%PDF-1.4 signed round trip"


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


@pytest.mark.asyncio
async def test_tool_mints_url_and_route_streams_it(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "e2e-client-secret-material")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "http://testserver")
    sd._signing_key.cache_clear()
    from core.config import get_transport_mode

    saved_mode = get_transport_mode()
    set_transport_mode("streamable-http")

    service = _gmail_service()
    creds = Mock(valid=True, expiry=datetime.utcnow() + timedelta(hours=1))
    store = Mock()
    store.get_credentials = lambda email: creds if email == "user@example.com" else None
    auth = AsyncMock(return_value=(service, "user@example.com"))

    try:
        with (
            patch.object(service_decorator, "_authenticate_service", auth),
            patch(
                "auth.oauth21_session_store.get_oauth21_session_store", lambda: store
            ),
            patch.object(sd, "build", lambda *a, **k: service),
        ):
            async with Client(server) as client:
                result = await client.call_tool(
                    "get_gmail_attachment_content",
                    {
                        "message_id": "msg-1",
                        "attachment_id": "att-1",
                        "user_google_email": "user@example.com",
                    },
                )
            text = result.content[0].text
            assert "streamed on demand" in text
            url = next(
                line.split("Download URL: ", 1)[1]
                for line in text.splitlines()
                if "Download URL:" in line
            )
            assert url.startswith("http://testserver/attachments/signed/")

            transport = httpx.ASGITransport(app=server.http_app())
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                ok = await http.get(url)
                tampered = await http.get(
                    url[:-2] + ("A" if url[-2] != "A" else "B") + url[-1]
                )

            assert ok.status_code == 200
            assert ok.content == PAYLOAD
            assert ok.headers["content-type"].startswith("application/pdf")
            assert 'filename="invoice.pdf"' in ok.headers["content-disposition"]
            assert tampered.status_code == 403
    finally:
        set_transport_mode(saved_mode)
        sd._signing_key.cache_clear()
