"""The ``chat`` signed-download source: Chat attachment bytes streamed from the
media endpoint, with the resource-name guards, redirect refusal and size cap.
Route tests go through the real app."""

import functools
from unittest.mock import Mock, patch

import httpx
import pytest
from google.oauth2.credentials import Credentials

import core.signed_downloads as sd
from core.server import server, set_transport_mode
import gchat.chat_tools  # noqa: F401  (the route serves a source only when its tools are loaded)

RN = "spaces/S/attachments/A"
USER = "user@example.com"
PAYLOAD = b"chat attachment bytes " * 100


@pytest.mark.parametrize(
    "bad",
    [
        "",
        None,
        "spaces/../x",
        "spaces/S?x=1",
        "spaces/S#frag",
        "spaces/S%2e%2e/x",
        "spaces\\S",
        "spaces/S\nattachments",
        "spaces/S\x00",
        "spaces/S\x7f",
    ],
)
def test_resource_name_guards(bad):
    with pytest.raises(ValueError):
        sd.chat_media_url(bad)


def test_url_is_pinned_to_the_chat_host():
    assert sd.chat_media_url(RN) == (
        "https://chat.googleapis.com/v1/media/spaces/S/attachments/A?alt=media"
    )
    # A name that looks like an authority or a scheme stays a path suffix.
    for name in ("@evil.example/x", "//evil.example/x", "https://evil.example/x"):
        assert httpx.URL(sd.chat_media_url(name)).host == "chat.googleapis.com"


def test_name_is_percent_encoded_as_a_path():
    assert "a%20b" in sd.chat_media_url("spaces/S/attachments/a b")


# --- fetcher, against a fake Chat endpoint ------------------------------------


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, then_raise=None):
        self._chunks, self._then_raise = chunks, then_raise

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._then_raise:
            raise self._then_raise


@pytest.fixture
def chat_endpoint(monkeypatch):
    """Route the fetcher's httpx client to ``handler``; records requests."""
    requests = []
    state = {"handler": None}

    def handler(request):
        requests.append(request)
        return state["handler"](request)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        sd.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.delenv("WORKSPACE_MCP_MAX_FILE_BYTES", raising=False)
    state["requests"] = requests
    return state


def _ok(chunks=(PAYLOAD,), length=True, then_raise=None):
    headers = {"content-length": str(sum(map(len, chunks)))} if length else {}
    return lambda request: httpx.Response(
        200, headers=headers, stream=_Stream(list(chunks), then_raise)
    )


CLAIMS = {"rn": RN, "fn": "r.pdf", "mt": "application/pdf"}
CREDS = Credentials(token="tok-123", refresh_token="r")


async def _drain(result):
    return b"".join([chunk async for chunk in result.stream])


@pytest.mark.asyncio
async def test_missing_rn_claim_is_a_fetch_error():
    with pytest.raises(sd.SignedDownloadError):
        await sd._fetch_chat({}, CREDS)


@pytest.mark.asyncio
async def test_streams_bytes_with_bearer_token_and_no_redirect_following(
    chat_endpoint,
):
    chat_endpoint["handler"] = _ok()
    result = await sd._fetch_chat(CLAIMS, CREDS)
    assert await _drain(result) == PAYLOAD
    assert (result.filename, result.media_type) == ("r.pdf", "application/pdf")
    request = chat_endpoint["requests"][0]
    assert request.headers["authorization"] == "Bearer tok-123"
    assert str(request.url) == sd.chat_media_url(RN)


@pytest.mark.asyncio
async def test_redirect_is_refused_not_followed(chat_endpoint):
    chat_endpoint["handler"] = lambda request: httpx.Response(
        302, headers={"location": "https://evil.example/x"}
    )
    with pytest.raises(sd.SignedDownloadError):
        await sd._fetch_chat(CLAIMS, CREDS)
    assert [str(r.url.host) for r in chat_endpoint["requests"]] == [
        "chat.googleapis.com"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_google_error_status_is_checked_before_streaming(chat_endpoint, status):
    chat_endpoint["handler"] = lambda request: httpx.Response(status, text="nope")
    with pytest.raises(sd.SignedDownloadError) as caught:
        await sd._fetch_chat(CLAIMS, CREDS)
    assert str(status) in str(caught.value)


@pytest.mark.asyncio
async def test_declared_length_over_the_cap_is_refused_up_front(
    chat_endpoint, monkeypatch
):
    monkeypatch.setenv("WORKSPACE_MCP_MAX_FILE_BYTES", "100")
    chat_endpoint["handler"] = _ok()
    with pytest.raises(sd.SignedDownloadError, match="size limit"):
        await sd._fetch_chat(CLAIMS, CREDS)


@pytest.mark.asyncio
async def test_undeclared_length_is_refused_before_streaming(chat_endpoint):
    chat_endpoint["handler"] = _ok(chunks=[b"x" * 60], length=False)
    with pytest.raises(sd.SignedDownloadError, match="Content-Length"):
        await sd._fetch_chat(CLAIMS, CREDS)


@pytest.mark.asyncio
async def test_mid_stream_cut_aborts_instead_of_ending_cleanly(chat_endpoint):
    chat_endpoint["handler"] = lambda request: httpx.Response(
        200,
        headers={"content-length": "100"},
        stream=_Stream([b"x" * 10], httpx.ReadError("connection reset")),
    )
    result = await sd._fetch_chat(CLAIMS, CREDS)
    with pytest.raises(sd.SignedDownloadError):
        await _drain(result)


@pytest.mark.asyncio
async def test_short_body_with_clean_end_is_an_error(chat_endpoint):
    chat_endpoint["handler"] = lambda request: httpx.Response(
        200, headers={"content-length": "100"}, stream=_Stream([b"x" * 10])
    )
    result = await sd._fetch_chat(CLAIMS, CREDS)
    with pytest.raises(sd.SignedDownloadError, match="ended early"):
        await _drain(result)


@pytest.mark.asyncio
async def test_body_longer_than_declared_is_an_error(chat_endpoint):
    chat_endpoint["handler"] = lambda request: httpx.Response(
        200, headers={"content-length": "5"}, stream=_Stream([b"x" * 10])
    )
    result = await sd._fetch_chat(CLAIMS, CREDS)
    with pytest.raises(sd.SignedDownloadError):
        await _drain(result)


# --- the real app: mint, then GET the link --------------------------------------


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "e2e-client-secret-material")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "http://testserver")
    sd._signing_key.cache_clear()
    from core.config import get_transport_mode

    saved_mode = get_transport_mode()
    set_transport_mode("streamable-http")
    try:
        with patch.object(
            sd, "_recover_credentials", Mock(return_value=CREDS)
        ) as creds:
            transport = httpx.ASGITransport(app=server.http_app())
            yield (
                functools.partial(
                    httpx.AsyncClient, transport=transport, base_url="http://testserver"
                ),
                creds,
            )
    finally:
        set_transport_mode(saved_mode)
        sd._signing_key.cache_clear()


def _link(**kw):
    kw.setdefault("ttl_seconds", 300)
    return sd.mint_url(
        source="chat",
        user_email=USER,
        ref={"rn": RN},
        filename="r.pdf",
        mime_type="application/pdf",
        **kw,
    )


@pytest.mark.asyncio
async def test_minted_chat_link_streams_the_attachment(app, chat_endpoint):
    client, creds = app
    chat_endpoint["handler"] = _ok()
    url = _link()
    assert url.startswith("http://testserver/attachments/signed/")
    async with client() as http:
        response = await http.get(url)
    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert response.headers["content-type"].startswith("application/pdf")
    assert 'filename="r.pdf"' in response.headers["content-disposition"]
    creds.assert_called_once_with(USER)
    # Nothing about the attachment was stored: only the one Chat call was made.
    assert len(chat_endpoint["requests"]) == 1


@pytest.mark.asyncio
async def test_tampered_and_expired_tokens_get_403_without_calling_google(
    app, chat_endpoint
):
    client, _ = app
    chat_endpoint["handler"] = _ok()
    url = _link()
    tampered = url[:-2] + ("A" if url[-2] != "A" else "B") + url[-1]
    async with client() as http:
        assert (await http.get(tampered)).status_code == 403
        assert (await http.get(_link(ttl_seconds=-1))).status_code == 403
    assert chat_endpoint["requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_google_refusal_is_a_502(app, chat_endpoint, status):
    client, _ = app
    chat_endpoint["handler"] = lambda request: httpx.Response(status, text="secret")
    async with client() as http:
        response = await http.get(_link())
    assert response.status_code == 502
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_oversized_attachment_is_refused(app, chat_endpoint, monkeypatch):
    client, _ = app
    monkeypatch.setenv("WORKSPACE_MCP_MAX_FILE_BYTES", "100")
    chat_endpoint["handler"] = _ok()
    async with client() as http:
        response = await http.get(_link())
    # The cap trips on the declared length, before any byte is sent.
    assert response.status_code == 502
