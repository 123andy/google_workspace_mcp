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


def chat_service(resource_name=RN, **extra):
    service = Mock()
    attachment = {
        "name": "spaces/S/messages/M/attachments/A",
        "contentName": "report.pdf",
        "contentType": "application/pdf",
        "source": "UPLOADED_CONTENT",
        **extra,
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

    assert "http://testserver/dl/" in text
    download.assert_not_called()


@pytest.mark.asyncio
async def test_unsignable_fails_loudly_without_downloading_or_storing(
    signed_env, tmp_path
):
    with (
        patch.object(sd, "offer_url", return_value=None),
        patch("gchat.chat_tools.download_http_url_bytes") as download,
    ):
        text = await call_tool()

    assert "no signed download link could be issued" in text
    assert "re-authenticate through this server" in text.lower()
    assert "start_google_auth" not in text
    assert "/attachments/" not in text and "/dl/" not in text
    download.assert_not_called()
    assert list(tmp_path.iterdir()) == []  # no disk write


@pytest.mark.asyncio
async def test_unsafe_resource_name_is_refused_before_minting(signed_env):
    with patch.object(sd, "offer_url") as offer:
        text = await call_tool(service=chat_service("spaces/S/attachments/../x"))

    assert text.startswith("Error:") and "invalid resource name" in text
    offer.assert_not_called()


def _nameless_service():
    service = chat_service(resource_name=None)
    message = service.spaces().messages().get().execute.return_value
    del message["attachment"][0]["name"]
    return service


@pytest.mark.asyncio
async def test_attachment_without_any_resource_name_is_an_error(signed_env):
    with patch.object(sd, "offer_url") as offer:
        text = await call_tool(service=_nameless_service())

    assert text == (
        "Error: cannot offer attachment 'report.pdf': it has no resource name."
    )
    offer.assert_not_called()


@pytest.mark.asyncio
async def test_attachment_without_any_resource_name_unchanged_when_off(
    signed_env, monkeypatch
):
    monkeypatch.delenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", raising=False)
    text = await call_tool(service=_nameless_service())

    assert text == "No resource name available for attachment 'report.pdf'."


@pytest.mark.asyncio
async def test_drive_file_attachment_is_an_error_and_mints_no_link(signed_env):
    with patch.object(sd, "offer_url") as offer:
        text = await call_tool(service=chat_service(resource_name=None))

    assert text.startswith("Error:")
    assert "Drive" in text
    assert "/dl/" not in text
    offer.assert_not_called()


@pytest.mark.asyncio
async def test_drive_source_is_recognised_even_with_a_data_ref(signed_env):
    service = chat_service(source="DRIVE_FILE", driveDataRef={"driveFileId": "1AbCdEf"})
    with patch.object(sd, "offer_url") as offer:
        text = await call_tool(service=service)

    assert text.startswith("Error:") and "Drive file" in text
    assert "1AbCdEf" in text
    offer.assert_not_called()


@pytest.mark.asyncio
async def test_drive_file_error_includes_the_id_when_present(signed_env):
    service = chat_service(
        resource_name=None,
        source="DRIVE_FILE",
        driveDataRef={"driveFileId": "1AbCdEf"},
    )
    text = await call_tool(service=service)

    assert "1AbCdEf" in text
    assert "/dl/" not in text


@pytest.mark.asyncio
async def test_drive_file_error_without_an_id_still_works(signed_env):
    text = await call_tool(service=chat_service(resource_name=None))

    assert text.startswith("Error:") and "Drive file" in text
    assert "File ID" not in text


@pytest.mark.asyncio
async def test_drive_file_attachment_unchanged_when_signing_is_off(
    signed_env, monkeypatch
):
    monkeypatch.delenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", raising=False)
    with (
        patch.object(sd, "offer_url") as offer,
        patch(
            "gchat.chat_tools.download_http_url_bytes",
            new_callable=AsyncMock,
            return_value=b"bytes",
        ) as download,
    ):
        await call_tool(service=chat_service(resource_name=None))

    offer.assert_not_called()
    download.assert_awaited_once()  # the legacy flow still attempts the name


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
async def test_minted_short_link_streams_the_attachment_through_the_app(signed_env):
    """Tool mints the /dl/ link; a plain GET through the real app streams the bytes."""
    import functools

    import httpx

    payload = b"chat attachment bytes " * 50
    real_client = httpx.AsyncClient
    seen = []

    def chat_handler(request):
        seen.append(request)
        return httpx.Response(200, content=payload)

    def client_factory(**kw):
        if kw.get("transport") is None and kw.get("follow_redirects") is False:
            kw["transport"] = httpx.MockTransport(chat_handler)
        return real_client(**kw)

    text = await call_tool()
    url = next(line.split(": ", 1)[1] for line in text.splitlines() if "/dl/" in line)
    assert url.startswith("http://testserver/dl/")

    creds = Mock(valid=True, expiry=datetime.utcnow() + timedelta(hours=1), token="t")
    with (
        patch.object(sd, "_recover_credentials", Mock(return_value=creds)),
        patch.object(sd.httpx, "AsyncClient", client_factory),
    ):
        transport = httpx.ASGITransport(app=server.http_app())
        async with functools.partial(
            real_client, transport=transport, base_url="http://testserver"
        )() as http:
            response = await http.get(url, follow_redirects=False)

    assert response.status_code == 200
    assert response.content == payload
    assert 'filename="report.pdf"' in response.headers["content-disposition"]
    assert [r.url.path for r in seen] == [f"/v1/media/{RN}"]
