"""download_chat_attachment hands out a signed link when signed downloads are on,
instead of buffering the file and returning an unsigned ``/attachments/{id}`` URL
that sits behind the gateway's login."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastmcp import Client

import auth.service_decorator as service_decorator
import core.signed_downloads as sd
from core.server import server, set_transport_mode
import gchat.chat_tools  # noqa: F401  (registers the tool under test)

OWNER = "user@example.com"
RN = "spaces/S/attachments/A"
PAYLOAD = b"%PDF-1.4 chat round trip"


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
async def test_flag_on_returns_signed_link_not_an_unsigned_url(signed_env):
    with patch(
        "gchat.chat_tools.download_http_url_bytes",
        new_callable=AsyncMock,
        return_value=b"bytes",
    ) as download:
        text = await call_tool()

    assert "http://testserver/attachments/signed/" in text
    assert "expire after 1 hour" not in text
    download.assert_not_called()  # nothing is fetched or stored at mint time


@pytest.mark.asyncio
async def test_link_carries_the_resource_name_and_file_details(signed_env):
    with patch.object(sd, "offer_url", wraps=sd.offer_url) as offer:
        text = await call_tool()

    kwargs = offer.call_args.kwargs
    assert kwargs["source"] == "chat"
    assert kwargs["ref"] == {"rn": RN}
    assert kwargs["filename"] == "report.pdf"
    assert kwargs["mime_type"] == "application/pdf"
    assert "File: report.pdf" in text and "~15 minutes" in text


@pytest.mark.asyncio
async def test_stateless_mode_returns_a_link(signed_env):
    with (
        patch("auth.oauth_config.is_stateless_mode", return_value=True),
        patch("gchat.chat_tools.download_http_url_bytes") as download,
    ):
        text = await call_tool()

    assert "http://testserver/attachments/signed/" in text
    download.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_unsignable_fails_loudly_without_downloading_or_storing(
    signed_env, tmp_path, stateless
):
    with (
        patch.object(
            sd,
            "offer_url",
            new_callable=AsyncMock,
            return_value=sd.Offer(reason=sd.NO_CREDENTIALS),
        ),
        patch("auth.oauth_config.is_stateless_mode", return_value=stateless),
        patch("gchat.chat_tools.download_http_url_bytes") as download,
    ):
        text = await call_tool()

    assert text.startswith("Error: no signed download link could be issued")
    assert sd.NO_CREDENTIALS in text
    assert "start_google_auth" not in text  # absent in OAuth 2.1 mode
    assert "/attachments/" not in text
    download.assert_not_called()
    assert list(tmp_path.iterdir()) == []  # no disk write


@pytest.mark.asyncio
async def test_unsafe_resource_name_is_refused_before_minting(signed_env):
    with patch.object(sd, "offer_url") as offer:
        text = await call_tool(service=chat_service("spaces/S/attachments/../x"))

    assert text.startswith("Error:")
    assert "invalid resource name" in text
    offer.assert_not_called()


@pytest.mark.asyncio
async def test_drive_file_attachment_gets_a_clear_error_and_no_link(signed_env):
    """A Drive file has no attachmentDataRef, and the media endpoint 400s on the
    attachment name, so minting a link would hand out one that cannot work."""
    with patch.object(sd, "offer_url") as offer:
        text = await call_tool(service=chat_service(resource_name=None))

    assert text.startswith("Error:")
    assert "Drive" in text
    offer.assert_not_called()
    assert "/attachments/signed/" not in text


@pytest.mark.asyncio
async def test_flag_off_keeps_the_unsigned_flow(signed_env, monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", raising=False)
    with (
        patch.object(sd, "offer_url") as offer,
        patch(
            "gchat.chat_tools.download_http_url_bytes",
            new_callable=AsyncMock,
            return_value=b"bytes",
        ) as download,
    ):
        text = await call_tool()

    offer.assert_not_called()
    download.assert_awaited_once()
    assert "expire after 1 hour" in text


@pytest.mark.asyncio
async def test_tool_mints_and_route_streams_the_attachment(signed_env, monkeypatch):
    """Tool call -> link -> GET through the real app -> bytes from the Chat media
    endpoint, with no cookies and nothing stored in between."""
    seen = []

    def chat_media(request):
        seen.append(request)
        return httpx.Response(200, content=PAYLOAD)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        sd.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(chat_media), **kw),
    )
    service = chat_service()
    with patch.object(
        sd, "_usable_credentials", AsyncMock(return_value=Mock(token="tok"))
    ):
        text = await call_tool(service=service)
        url = next(
            line.split("Download URL: ", 1)[1]
            for line in text.splitlines()
            if "Download URL:" in line
        )
        transport = httpx.ASGITransport(app=server.http_app())
        async with real_client(
            transport=transport, base_url="http://testserver"
        ) as http:
            response = await http.get(url)

    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert 'filename="report.pdf"' in response.headers["content-disposition"]
    assert [str(r.url) for r in seen] == [sd.chat_media_url(RN)]
    assert seen[0].headers["authorization"] == "Bearer tok"
