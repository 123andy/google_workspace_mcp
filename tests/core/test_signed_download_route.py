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
async def test_tool_mints_url_and_route_streams_it(monkeypatch, tmp_path):
    monkeypatch.setattr("core.attachment_storage.STORAGE_DIR", tmp_path)  # never $HOME
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", "true")
    # This fork shortens links to /dl/{handle} by default (core.download_handles);
    # these tests pin the long form. The short form has its own round trip in
    # tests/core/test_download_handles.py.
    monkeypatch.setenv("WORKSPACE_MCP_SHORT_SIGNED_URLS", "false")
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


@pytest.mark.asyncio
async def test_legacy_mode_credentials_only_in_the_credential_store_round_trip(
    monkeypatch, tmp_path
):
    """Legacy / trusted-gateway shape: after a restart the in-process session store
    is empty and the owner's credentials live only in the persistent credential
    store. The tool must still mint, and the route must serve — reading the store
    and writing nothing back to either store."""
    import auth.credential_store as credential_store
    from auth.credential_store import LocalDirectoryCredentialStore
    from google.oauth2.credentials import Credentials

    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", "true")
    # This fork shortens links to /dl/{handle} by default (core.download_handles);
    # these tests pin the long form. The short form has its own round trip in
    # tests/core/test_download_handles.py.
    monkeypatch.setenv("WORKSPACE_MCP_SHORT_SIGNED_URLS", "false")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "e2e-client-secret-material")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "http://testserver")
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: False)
    # Should the tool ever fall back to disk, it must land in the temp dir, not the
    # user's ~/.workspace-mcp/attachments.
    (tmp_path / "attachments").mkdir()
    monkeypatch.setattr("core.attachment_storage.STORAGE_DIR", tmp_path / "attachments")
    sd._signing_key.cache_clear()
    from core.config import get_transport_mode

    saved_mode = get_transport_mode()
    set_transport_mode("streamable-http")

    # A real local credential store, pointed at a temp dir (never a user directory).
    store = LocalDirectoryCredentialStore(base_dir=str(tmp_path))
    store.store_credential(
        "user@example.com",
        Credentials(
            token="ya29.from-store",
            refresh_token=None,
            token_uri="https://oauth2.googleapis.com/token",
            client_id="cid",
            client_secret="csecret",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            expiry=datetime.utcnow() + timedelta(hours=1),
        ),
    )
    monkeypatch.setattr(
        store, "store_credential", Mock(side_effect=AssertionError("route wrote"))
    )
    monkeypatch.setattr(credential_store, "_credential_store", store)

    empty_session_store = Mock()
    empty_session_store.get_credentials = lambda email: None
    empty_session_store.store_session = Mock(side_effect=AssertionError("route wrote"))

    service = _gmail_service()
    auth = AsyncMock(return_value=(service, "user@example.com"))
    used = {}

    def build(*args, **kwargs):
        used["token"] = kwargs["credentials"].token
        return service

    try:
        with (
            patch.object(service_decorator, "_authenticate_service", auth),
            patch(
                "auth.oauth21_session_store.get_oauth21_session_store",
                lambda: empty_session_store,
            ),
            patch.object(sd, "build", build),
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
            assert "streamed on demand" in text, text
            url = next(
                line.split("Download URL: ", 1)[1]
                for line in text.splitlines()
                if "Download URL:" in line
            )

            transport = httpx.ASGITransport(app=server.http_app())
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                ok = await http.get(url)

            assert ok.status_code == 200
            assert ok.content == PAYLOAD
            assert used["token"] == "ya29.from-store"
            assert ok.headers["x-content-type-options"] == "nosniff"
            assert ok.headers["cache-control"] == "no-store"
            store.store_credential.assert_not_called()
            empty_session_store.store_session.assert_not_called()
            assert len(list(tmp_path.glob("*.json"))) == 1  # only the setup write
            assert list((tmp_path / "attachments").iterdir()) == []  # no disk fallback
    finally:
        set_transport_mode(saved_mode)
        sd._signing_key.cache_clear()


def _part(mime, name, aid, size):
    return {
        "mimeType": mime,
        "filename": name,
        "body": {"attachmentId": aid, "size": size},
    }


# Metadata as Gmail returns it on a later fetch: the IDs the caller holds
# (``old-*``) have rotated, so only the listing's ordinal still identifies the PDF.
SCANNER_MAIL = {
    "payload": {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/related",
                "parts": [
                    _part("image/png", "image001.png", "new-img1", 4210),
                    _part("image/png", "image002.png", "new-img2", 3890),
                    _part("image/jpeg", "image003.jpg", "new-img3", 9012),
                ],
            },
            _part("application/pdf", "BRN94DDF87494B4_006201.pdf", "new-pdf", 26),
        ],
    }
}
NAMELESS_PART = {
    "payload": {
        "mimeType": "application/octet-stream",
        "body": {"attachmentId": "new-1", "size": 26},
    }
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata, args, served_as",
    [
        (
            SCANNER_MAIL,
            {"attachment_id": "old-pdf", "attachment_index": 3},
            "BRN94DDF87494B4_006201.pdf",
        ),
        (NAMELESS_PART, {"attachment_id": "old-1"}, "attachment"),
    ],
    ids=["rotated-ids-named-by-index", "nameless-part"],
)
async def test_content_disposition_carries_the_resolved_name(
    monkeypatch, tmp_path, metadata, args, served_as
):
    """The name the tool prints is the name the route serves: resolved by the
    listing's index once Gmail has rotated the IDs, and the documented
    'attachment' fallback when the part has no name at all."""
    monkeypatch.setattr("core.attachment_storage.STORAGE_DIR", tmp_path)  # never $HOME
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "e2e-client-secret-material")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_MAX_FILE_BYTES", raising=False)
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "http://testserver")
    sd._signing_key.cache_clear()
    from core.config import get_transport_mode

    saved_mode = get_transport_mode()
    set_transport_mode("streamable-http")

    service = _gmail_service()
    service.users().messages().get().execute.return_value = metadata
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
                        "user_google_email": "user@example.com",
                        **args,
                    },
                )
            text = result.content[0].text
            assert f"Filename: {served_as}" in text, text
            assert "unknown" not in text
            url = next(
                line.split("Download URL: ", 1)[1]
                for line in text.splitlines()
                if "Download URL:" in line
            )
            transport = httpx.ASGITransport(app=server.http_app())
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                ok = await http.get(url)

            assert ok.status_code == 200
            assert ok.content == PAYLOAD
            assert f'filename="{served_as}"' in ok.headers["content-disposition"]
    finally:
        set_transport_mode(saved_mode)
        sd._signing_key.cache_clear()
