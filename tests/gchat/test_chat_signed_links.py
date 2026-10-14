"""download_chat_attachment hands out a signed ``/dl/{handle}`` link when signed
downloads are on, instead of buffering the file and returning an unsigned
``/attachments/{id}`` URL that sits behind the gateway's login."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastmcp import Client

import auth.service_decorator as service_decorator
import core.download_handles as dh
import core.signed_downloads as sd
from core.server import server, set_transport_mode
import gchat.chat_tools  # noqa: F401  (registers the tool under test)

OWNER = "user@example.com"
RN = "spaces/S/attachments/A"


def chat_service(resource_name=RN):
    service = Mock()
    attachment = {
        "name": "spaces/S/messages/M/attachments/A",
        "contentName": "report.pdf",
        "contentType": "application/pdf",
        "source": "UPLOADED_CONTENT",
    }
    if resource_name:
        attachment["attachmentDataRef"] = {"resourceName": resource_name}
    service.spaces().messages().get().execute.return_value = {
        "name": "spaces/S/messages/M",
        "attachment": [attachment],
    }
    service._http.credentials.token = "fake-token"
    return service


def owner_patches(service):
    creds = Mock(valid=True, expiry=datetime.utcnow() + timedelta(hours=1))
    store = Mock()
    store.get_credentials = lambda email: creds if email == OWNER else None
    return (
        patch.object(
            service_decorator,
            "_authenticate_service",
            AsyncMock(return_value=(service, OWNER)),
        ),
        patch("auth.oauth21_session_store.get_oauth21_session_store", lambda: store),
    )


@pytest.fixture
def signed_env(monkeypatch, tmp_path):
    monkeypatch.setattr("core.attachment_storage.STORAGE_DIR", tmp_path)  # never $HOME
    monkeypatch.setattr(dh, "_store", None)
    monkeypatch.setattr(dh, "_store_built", False)
    monkeypatch.delenv(dh.FLAG_ENV, raising=False)
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


async def call_tool(service=None, **args):
    service = service or chat_service()
    a, b = owner_patches(service)
    with a, b:
        async with Client(server) as client:
            result = await client.call_tool(
                "download_chat_attachment",
                {
                    "message_id": "spaces/S/messages/M",
                    "user_google_email": OWNER,
                    **args,
                },
            )
    return result.content[0].text


@pytest.mark.asyncio
async def test_flag_on_returns_signed_short_link_not_an_unsigned_url(signed_env):
    with patch(
        "gchat.chat_tools.download_http_url_bytes",
        new_callable=AsyncMock,
        return_value=b"bytes",
    ) as download:
        text = await call_tool()

    assert "http://testserver/dl/" in text
    assert "http://testserver/attachments/" not in text
    assert "expire after 1 hour" not in text
    download.assert_not_called()  # nothing is fetched or stored at mint time
