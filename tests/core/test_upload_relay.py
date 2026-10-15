"""``/ul/{handle}``: a PUT relayed to a Google resumable session (core.upload_relay).

Route tests go through ``server.http_app()`` (the real middleware stack) with
Google replaced by an ``httpx.MockTransport``; the real-socket behaviour is in
test_upload_relay_live.py."""

import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
import pytest_asyncio
from google.oauth2.credentials import Credentials
from key_value.aio.stores.memory import MemoryStore

import core.server as core_server
import core.signed_downloads as sd
import core.upload_relay as ur
from core.server import server, set_transport_mode

USER = "user@example.com"
GURL = (
    "https://www.googleapis.com/upload/drive/v3/files"
    "?uploadType=resumable&supportsAllDrives=true&upload_id=ABCdef123_secret"
)
EXTERNAL = "https://mcp.example.com"


# --- session URL validation ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        GURL,
        "https://www.googleapis.com:443/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com/upload/drive/v3/files/FILEID"
        "?uploadType=resumable&upload_id=X",
    ],
)
def test_session_url_accepts_googles_upload_endpoint(url):
    assert ur.is_valid_session_url(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        42,
        "http://www.googleapis.com/upload/drive/v3/files?upload_id=X",
        "https://evil.example/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com.evil.example/upload/drive/v3/files?upload_id=X",
        "https://evilwww.googleapis.com/upload/drive/v3/files?upload_id=X",
        "https://storage.googleapis.com/upload/drive/v3/files?upload_id=X",
        "https://user@www.googleapis.com/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com@evil.example/upload/drive/v3/files?upload_id=X",
        "https://user:pw@www.googleapis.com/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com:8443/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com:80/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com\\@evil.example/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com/upload\\drive/v3/files?upload_id=X",
        "https://www.googleapis.com./upload/drive/v3/files?upload_id=X",
        "https://WWW.GOOGLEAPIS.COM/upload/drive/v3/files?upload_id=X",
        "https://www.googľeapis.com/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=é",
        "https://xn--www-googleapis-com.example/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis%2Ecom/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=X#frag",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=X\r\nHost: x",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=X Y",
        " https://www.googleapis.com/upload/drive/v3/files?upload_id=X",
        "https://www.googleapis.com/drive/v3/files?upload_id=X",
        "https://www.googleapis.com/upload/storage/v1/b/x/o?upload_id=X",
        "https://www.googleapis.com/upload/drive/v3/files/../../../evil?upload_id=X",
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=" + "x" * 5000,
        "//www.googleapis.com/upload/drive/v3/files?upload_id=X",
    ],
)
def test_session_url_refuses_everything_else(url):
    assert not ur.is_valid_session_url(url)


# --- startup ---------------------------------------------------------------------


def test_startup_off_or_stdio_checks_nothing(monkeypatch):
    monkeypatch.delenv(ur.FLAG_ENV, raising=False)
    ur.validate_startup("streamable-http")
    monkeypatch.setenv(ur.FLAG_ENV, "true")
    monkeypatch.delenv(sd.FLAG_ENV, raising=False)
    ur.validate_startup("stdio")


def test_startup_needs_signed_downloads(monkeypatch):
    monkeypatch.setenv(ur.FLAG_ENV, "true")
    monkeypatch.delenv(sd.FLAG_ENV, raising=False)
    with pytest.raises(ValueError, match=sd.FLAG_ENV):
        ur.validate_startup("streamable-http")


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.5", "100MiB"])
def test_startup_rejects_a_bad_cap(monkeypatch, raw):
    monkeypatch.setenv(ur.FLAG_ENV, "true")
    monkeypatch.setenv(sd.FLAG_ENV, "true")
    monkeypatch.setenv(ur.MAX_BYTES_ENV, raw)
    with pytest.raises(ValueError, match=ur.MAX_BYTES_ENV):
        ur.validate_startup("streamable-http")


@pytest.mark.parametrize("raw", ["true", "TRUE", "True", " true", "true ", "1", ""])
def test_flag_parses_like_the_download_flag(monkeypatch, raw):
    monkeypatch.setenv(ur.FLAG_ENV, raw)
    monkeypatch.setenv(sd.FLAG_ENV, raw)
    assert ur.flag_set() == sd._flag_set()


def test_cap_default_and_override(monkeypatch):
    monkeypatch.delenv(ur.MAX_BYTES_ENV, raising=False)
    assert ur.max_bytes() == 100 * 1024 * 1024
    monkeypatch.setenv(ur.MAX_BYTES_ENV, "2048")
    assert ur.max_bytes() == 2048


def test_stdio_operator_is_told_the_setting_is_ignored(monkeypatch, caplog):
    monkeypatch.setenv(ur.FLAG_ENV, "true")
    with caplog.at_level(logging.WARNING, logger="core.upload_relay"):
        ur.log_if_ignored("stdio")
    assert "ignored" in caplog.text


# --- fixtures ------------------------------------------------------------------


@pytest.fixture
def relay_env(monkeypatch):
    """Upload links on over streamable-http, fresh in-process store, owner usable."""
    monkeypatch.setenv(sd.FLAG_ENV, "true")
    monkeypatch.setenv(ur.FLAG_ENV, "true")
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", EXTERNAL)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "relay-test-client-secret")
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.delenv(ur.MAX_BYTES_ENV, raising=False)
    sd._signing_key.cache_clear()
    monkeypatch.setattr(ur, "_store", MemoryStore())
    monkeypatch.setattr(ur, "_store_built", True)
    monkeypatch.setattr(ur, "_in_flight", set())
    creds = Credentials(token="t", refresh_token="r")
    monkeypatch.setattr(sd, "_recover_credentials", Mock(return_value=creds))
    from core.config import get_transport_mode

    saved = get_transport_mode()
    set_transport_mode("streamable-http")
    yield
    set_transport_mode(saved)
    sd._signing_key.cache_clear()


@pytest.fixture
def mounted(relay_env):
    """The route as configure_server_for_http mounts it; removed afterwards."""
    before = list(server._additional_http_routes)
    core_server._upload_relay_route_registered = False
    core_server._ensure_upload_relay_route()
    yield
    server._additional_http_routes[:] = before
    core_server._upload_relay_route_registered = False


class FakeGoogle:
    """Stands in for the session URL; records what reached it."""

    def __init__(self):
        self.requests = []
        self.bodies = []
        self.reply = lambda request, body: httpx.Response(
            200, json={"id": "FILE1", "name": "x.pdf"}
        )

    async def handler(self, request):
        body = await request.aread()
        self.requests.append(request)
        self.bodies.append(body)
        result = self.reply(request, body)
        if asyncio.iscoroutine(result):
            result = await result
        return result


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        ur.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(fake.handler), **kw),
    )
    return fake


@pytest_asyncio.fixture
async def client(mounted):
    transport = httpx.ASGITransport(app=server.http_app())
    async with httpx.AsyncClient(transport=transport, base_url=EXTERNAL) as http:
        yield http


async def _handle(mime="application/pdf"):
    handle = await ur.store_session(GURL, USER, mime)
    assert handle
    return handle


async def _record(handle):
    return await ur._store.get(handle, collection=ur._COLLECTION)


# --- store -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_round_trip_and_fail_closed_loads(relay_env):
    handle = await _handle()
    assert len(handle) >= 22
    record = await ur._load(handle)
    assert record["gurl"] == GURL and record["sub"] == USER
    assert record["state"] == "new" and record["reqs"] == 0
    assert record["hard_exp"] - record["exp"] == pytest.approx(
        ur._MAX_LIFETIME_SECONDS - ur._FIRST_USE_SECONDS, abs=2
    )
    for bad in ("", "short", "x" * 65, "has/slash" + "x" * 20, None):
        assert await ur._load(bad) is None


@pytest.mark.asyncio
async def test_store_refuses_an_invalid_session_url(relay_env):
    assert (
        await ur.store_session("https://evil.example/upload?upload_id=1", USER, "a/b")
        is None
    )
    assert await ur.store_session(GURL, "", "a/b") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"exp": 0},
        {"hard_exp": 0},
        {"gurl": "https://evil.example/upload/drive/v3/files?upload_id=X"},
        {"sub": ""},
        {"state": "weird"},
        {"bytes": "1"},
        {"bytes": -1},
        {"reqs": True},
        {"mt": 5},
        {"mt": "text/pl\u00e4in"},
        {"mt": "text/plain\r\nX-Evil: 1"},
        {"hard_exp": time.time() + 10 * 3600},
        {"reply": {"status": 500, "body": "", "type": ""}},
        {"reply": {"status": 200, "body": "x" * 70_000, "type": ""}},
    ],
)
async def test_poisoned_or_expired_records_are_unknown(relay_env, change):
    handle = await _handle()
    record = await _record(handle)
    record.update(change)
    await ur._store.put(handle, record, collection=ur._COLLECTION)
    assert await ur._load(handle) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime", ["", None, 7, "a\u00e9/b", "text/plain\r\n", " text/plain"]
)
async def test_store_refuses_a_mime_type_unfit_for_a_header(relay_env, mime):
    assert await ur.store_session(GURL, USER, mime) is None


@pytest.mark.asyncio
async def test_store_write_failure_gives_no_handle(relay_env, monkeypatch):
    monkeypatch.setattr(ur._store, "put", AsyncMock(side_effect=RuntimeError("down")))
    assert await ur.store_session(GURL, USER, "a/b") is None


# --- route: success, headers, streaming --------------------------------------------


@pytest.mark.asyncio
async def test_full_put_is_relayed_and_closes_the_link(client, google):
    handle = await _handle()
    payload = bytes(range(256)) * 400
    response = await client.put(
        f"/ul/{handle}",
        content=payload,
        headers={
            "Content-Type": "text/evil",
            "Authorization": "Bearer client-token",
            "Cookie": "session=abc",
            "X-Forwarded-For": "1.2.3.4",
            "X-Upload-Content-Type": "text/evil",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"id": "FILE1", "name": "x.pdf"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"

    (sent,) = google.requests
    assert str(sent.url) == GURL and sent.method == "PUT"
    assert google.bodies == [payload]
    names = {k.lower() for k in sent.headers.keys()}
    assert names <= {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "content-length",
        "content-type",
    }
    assert sent.headers["content-type"] == "application/pdf"  # the minted type
    assert sent.headers["content-length"] == str(len(payload))
    assert "transfer-encoding" not in names

    record = await _record(handle)
    assert (record["state"], record["reqs"], record["bytes"]) == (
        "done",
        1,
        len(payload),
    )
    again = await client.put(f"/ul/{handle}", content=b"x")
    assert again.status_code == 410
    assert len(google.requests) == 1


@pytest.mark.asyncio
async def test_post_is_accepted_and_sent_on_as_put(client, google):
    handle = await _handle()
    response = await client.post(f"/ul/{handle}", content=b"abc")
    assert response.status_code == 200
    assert google.requests[0].method == "PUT"


@pytest.mark.asyncio
async def test_other_methods_are_refused(client, google):
    handle = await _handle()
    for method in ("GET", "DELETE", "PATCH"):
        response = await client.request(method, f"/ul/{handle}")
        assert response.status_code == 405
    assert google.requests == []


@pytest.mark.asyncio
async def test_google_response_headers_are_allowlisted(client, google):
    google.reply = lambda request, body: httpx.Response(
        201,
        content=b'{"id":"F"}',
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=LEAK",
            "X-GUploader-UploadID": "LEAK",
            "Set-Cookie": "g=1",
            "ETag": "abc",
        },
    )
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"abc")
    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json; charset=UTF-8"
    for leaked in ("location", "x-guploader-uploadid", "set-cookie", "etag"):
        assert leaked not in response.headers
    assert "LEAK" not in str(response.headers)


@pytest.mark.asyncio
async def test_oversized_google_reply_is_not_relayed(client, google):
    google.reply = lambda request, body: httpx.Response(200, content=b"x" * 70_000)
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"abc")
    assert response.status_code == 200
    assert response.json()["status"] == "uploaded"
    assert (await _record(handle))["state"] == "done"


# --- route: resumable pass-through ------------------------------------------------


@pytest.mark.asyncio
async def test_resume_incomplete_passes_through_with_its_range(client, google):
    google.reply = lambda request, body: httpx.Response(
        308, headers={"Range": "bytes=0-262143", "X-GUploader-UploadID": "LEAK"}
    )
    handle = await _handle()
    chunk = b"y" * 262144
    response = await client.put(
        f"/ul/{handle}",
        content=chunk,
        headers={"Content-Range": "bytes 0-262143/1000000"},
    )
    assert response.status_code == 308
    assert response.headers["range"] == "bytes=0-262143"
    assert "x-guploader-uploadid" not in response.headers
    assert google.requests[0].headers["content-range"] == "bytes 0-262143/1000000"
    record = await _record(handle)
    assert record["state"] == "active" and record["bytes"] == len(chunk)


@pytest.mark.asyncio
async def test_status_query_without_range_and_a_bad_range_is_dropped(client, google):
    google.reply = lambda request, body: httpx.Response(308, headers={"Range": "junk"})
    handle = await _handle()
    response = await client.put(
        f"/ul/{handle}", content=b"", headers={"Content-Range": "bytes */1000"}
    )
    assert response.status_code == 308
    assert "range" not in response.headers
    assert google.requests[0].headers["content-length"] == "0"
    assert google.bodies == [b""]


@pytest.mark.asyncio
async def test_status_query_without_a_size_says_what_is_missing(client, google):
    handle = await _handle()
    response = await client.put(
        f"/ul/{handle}", content=b"", headers={"Content-Range": "bytes */*"}
    )
    assert response.status_code == 400
    assert "bytes */<total size>" in response.json()["error"]
    assert google.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_range, body, status",
    [
        ("bytes 0-9", b"x" * 10, 400),
        ("bytes=0-9/10", b"x" * 10, 400),
        ("bytes 5-1/10", b"x" * 10, 400),
        ("bytes 0-9/5", b"x" * 10, 400),
        ("bytes 0-9/100", b"x" * 5, 400),
        ("bytes */100", b"x", 400),
        ("bytes */*", b"", 400),
        ("bytes 0-9/2000", b"x" * 10, 413),
        ("bytes 1000-1009/*", b"x" * 10, 413),
    ],
)
async def test_content_range_is_checked_before_google(
    client, google, monkeypatch, content_range, body, status
):
    monkeypatch.setenv(ur.MAX_BYTES_ENV, "1000")
    handle = await _handle()
    response = await client.put(
        f"/ul/{handle}", content=body, headers={"Content-Range": content_range}
    )
    assert response.status_code == status
    assert google.requests == []


# --- route: limits before any Google call ---------------------------------------


@pytest.mark.asyncio
async def test_over_cap_is_413_before_google(client, google, monkeypatch):
    monkeypatch.setenv(ur.MAX_BYTES_ENV, "1000")
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"x" * 1001)
    assert response.status_code == 413
    assert google.requests == []
    assert (await _record(handle))["reqs"] == 0  # not counted


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "POST"])
async def test_empty_body_without_content_range_is_400_before_google(
    client, google, method
):
    """Sent on, an empty PUT would complete the session as an empty file (for
    update_drive_file: blank the file)."""
    handle = await _handle()
    response = await client.request(method, f"/ul/{handle}", content=b"")
    assert response.status_code == 400
    assert "Content-Range" in response.json()["error"]
    assert google.requests == []
    record = await _record(handle)
    assert (record["state"], record["reqs"]) == ("new", 0)
    # The status query form is still relayed.
    google.reply = lambda request, body: httpx.Response(308)
    query = await client.put(
        f"/ul/{handle}", content=b"", headers={"Content-Range": "bytes */10"}
    )
    assert query.status_code == 308 and len(google.requests) == 1


@pytest.mark.asyncio
async def test_chunked_body_is_411(client, google):
    handle = await _handle()

    async def gen():
        yield b"abc"

    response = await client.put(f"/ul/{handle}", content=gen())
    assert response.status_code == 411
    assert google.requests == []


@pytest.mark.asyncio
async def test_missing_content_length_is_411(relay_env, google):
    handle = await _handle()
    scope = {
        "type": "http",
        "method": "PUT",
        "path": f"/ul/{handle}",
        "headers": [],
        "path_params": {"handle": handle},
        "query_string": b"",
    }
    from starlette.requests import Request

    response = await ur.serve(Request(scope))
    assert response.status_code == 411
    assert google.requests == []


@pytest.mark.asyncio
async def test_malformed_content_length_is_400(relay_env, google):
    from starlette.requests import Request

    handle = await _handle()
    for value in (b"12a", b"-1", b"\xd9\xa1"):
        scope = {
            "type": "http",
            "method": "PUT",
            "path": f"/ul/{handle}",
            "headers": [(b"content-length", value)],
            "path_params": {"handle": handle},
            "query_string": b"",
        }
        response = await ur.serve(Request(scope))
        assert response.status_code == 400
    assert google.requests == []


@pytest.mark.asyncio
async def test_total_volume_is_capped_at_twice_the_cap(client, google, monkeypatch):
    monkeypatch.setenv(ur.MAX_BYTES_ENV, "1000")
    handle = await _handle()
    record = await _record(handle)
    record["bytes"] = 1500
    await ur._store.put(handle, record, collection=ur._COLLECTION)
    response = await client.put(f"/ul/{handle}", content=b"x" * 501)
    assert response.status_code == 413
    response = await client.put(f"/ul/{handle}", content=b"x" * 500)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_request_count_is_bounded(client, google):
    handle = await _handle()
    record = await _record(handle)
    record["reqs"] = ur._MAX_REQUESTS
    await ur._store.put(handle, record, collection=ur._COLLECTION)
    response = await client.put(f"/ul/{handle}", content=b"x")
    assert response.status_code == 410
    assert google.requests == []


@pytest.mark.asyncio
async def test_unknown_malformed_and_expired_handles_answer_alike(client, google):
    handle = await _handle()
    record = await _record(handle)
    record["exp"] = time.time() - 1
    await ur._store.put(handle, record, collection=ur._COLLECTION)
    bodies = set()
    for path in (f"/ul/{handle}", "/ul/" + "A" * 22, "/ul/bad!handle"):
        response = await client.put(path, content=b"x")
        assert response.status_code == 403
        bodies.add(response.content)
    assert len(bodies) == 1
    assert google.requests == []


@pytest.mark.asyncio
async def test_owner_without_usable_credentials_is_refused(client, google, monkeypatch):
    monkeypatch.setattr(sd, "_recover_credentials", Mock(return_value=None))
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"x")
    assert response.status_code == 401
    assert google.requests == []
    assert handle not in ur._in_flight


# --- route: Google failures ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 410])
async def test_google_4xx_closes_the_link(client, google, status):
    google.reply = lambda request, body: httpx.Response(status, text="nope LEAK")
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"x")
    assert response.status_code == 410
    assert "call the tool again" in response.json()["error"].lower()
    assert "LEAK" not in response.text
    assert (await _record(handle))["state"] == "dead"
    again = await client.put(f"/ul/{handle}", content=b"x")
    assert again.status_code == 410
    assert len(google.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429])
async def test_google_retry_statuses_keep_the_link(client, google, status):
    google.reply = lambda request, body: httpx.Response(status)
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"x")
    assert response.status_code == 503
    assert (await _record(handle))["state"] == "active"


@pytest.mark.asyncio
async def test_google_5xx_is_502_and_keeps_the_link(client, google):
    google.reply = lambda request, body: httpx.Response(503, text="LEAK")
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"x")
    assert response.status_code == 502
    assert "LEAK" not in response.text
    assert (await _record(handle))["state"] == "active"
    google.reply = lambda request, body: httpx.Response(200, json={"id": "F"})
    assert (await client.put(f"/ul/{handle}", content=b"x")).status_code == 200


@pytest.mark.asyncio
async def test_redirects_are_not_followed(client, google):
    google.reply = lambda request, body: httpx.Response(
        302, headers={"Location": "https://evil.example/steal"}
    )
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"x")
    assert response.status_code == 502
    assert "location" not in response.headers
    assert [str(r.url) for r in google.requests] == [GURL]


@pytest.mark.asyncio
async def test_google_timeout_is_504(client, google):
    def reply(request, body):
        raise httpx.ReadTimeout("slow", request=request)

    google.reply = reply
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"x")
    assert response.status_code == 504
    assert (await _record(handle))["state"] == "active"


@pytest.mark.asyncio
async def test_google_connection_failure_is_502(client, google):
    def reply(request, body):
        raise httpx.ConnectError("refused", request=request)

    google.reply = reply
    handle = await _handle()
    response = await client.put(f"/ul/{handle}", content=b"x")
    assert response.status_code == 502


# --- route: one relay at a time ---------------------------------------------------


@pytest.mark.asyncio
async def test_second_concurrent_request_is_409(client, google):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request, body):
        entered.set()
        await release.wait()
        return httpx.Response(308, headers={"Range": "bytes=0-0"})

    google.reply = slow
    handle = await _handle()
    first = asyncio.create_task(
        client.put(
            f"/ul/{handle}", content=b"x", headers={"Content-Range": "bytes 0-0/10"}
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    second = await client.put(f"/ul/{handle}", content=b"x")
    assert second.status_code == 409
    release.set()
    assert (await first).status_code == 308
    assert handle not in ur._in_flight
    assert len(google.requests) == 1


@pytest.mark.asyncio
async def test_record_is_read_only_once_the_handle_is_held(client, google, monkeypatch):
    """A second request must not read the record while the first is still
    running: it would act on a stale copy (a link already done, or counters
    about to be overwritten)."""
    handle = await _handle()
    real_get = ur._store.get
    loading, release = asyncio.Event(), asyncio.Event()

    async def slow_get(key, collection=None):
        record = await real_get(key, collection=collection)
        if not loading.is_set():
            loading.set()
            await release.wait()
        return record

    monkeypatch.setattr(ur._store, "get", slow_get)
    first = asyncio.create_task(client.put(f"/ul/{handle}", content=b"abcd"))
    await asyncio.wait_for(loading.wait(), 5)
    second = await client.put(f"/ul/{handle}", content=b"ab")
    assert second.status_code == 409
    release.set()
    assert (await first).status_code == 200
    assert len(google.requests) == 1
    record = await _record(handle)
    assert (record["state"], record["reqs"], record["bytes"]) == ("done", 1, 4)


@pytest.mark.asyncio
async def test_unexpected_failure_is_502_without_a_traceback(client, google, caplog):
    def reply(request, body):
        raise RuntimeError("boom " + str(request.url))

    google.reply = reply
    handle = await _handle()
    with caplog.at_level(logging.DEBUG, logger="core.upload_relay"):
        response = await client.put(f"/ul/{handle}", content=b"x")
    assert response.status_code == 502
    assert handle not in ur._in_flight
    assert "ABCdef123_secret" not in caplog.text
    assert (await _record(handle))["state"] == "active"


# --- a completed link -------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_query_on_a_completed_link_replays_googles_reply(client, google):
    google.reply = lambda request, body: httpx.Response(
        201, json={"id": "FILE9"}, headers={"Content-Type": "application/json"}
    )
    handle = await _handle()
    assert (await client.put(f"/ul/{handle}", content=b"abc")).status_code == 201
    query = await client.put(
        f"/ul/{handle}", content=b"", headers={"Content-Range": "bytes */3"}
    )
    assert query.status_code == 201
    assert query.json() == {"id": "FILE9"}
    assert query.headers["content-type"] == "application/json"
    assert query.headers["cache-control"] == "no-store"
    assert len(google.requests) == 1  # answered from the record, not Google
    for other in (
        {"content": b"x"},
        {"content": b"", "headers": {"Content-Range": "bytes */*"}},
        {"content": b"x", "headers": {"Content-Range": "bytes 0-0/3"}},
    ):
        closed = await client.put(f"/ul/{handle}", **other)
        assert closed.status_code == 410
        # A caller that lost the 201 must not upload a second copy.
        message = closed.json()["error"]
        assert "completed; do not upload again" in message
        assert "'Content-Range: bytes */<total size>'" in message
    assert len(google.requests) == 1


@pytest.mark.asyncio
async def test_completed_link_without_a_kept_reply_says_so(client, google):
    google.reply = lambda request, body: httpx.Response(200, content=b"x" * 70_000)
    handle = await _handle()
    assert (await client.put(f"/ul/{handle}", content=b"abc")).status_code == 200
    query = await client.put(
        f"/ul/{handle}", content=b"", headers={"Content-Range": "bytes */3"}
    )
    assert query.status_code == 410
    assert "do not upload again" in query.json()["error"]
    assert "not kept" in query.json()["error"]
    assert len(google.requests) == 1


# --- lifetime ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_use_slides_the_expiry_but_never_past_the_hard_limit(client, google):
    google.reply = lambda request, body: httpx.Response(308)
    handle = await _handle()
    record = await _record(handle)
    record["hard_exp"] = time.time() + 60
    await ur._store.put(handle, record, collection=ur._COLLECTION)
    await client.put(
        f"/ul/{handle}", content=b"x", headers={"Content-Range": "bytes 0-0/10"}
    )
    after = await _record(handle)
    assert after["exp"] == pytest.approx(record["hard_exp"], abs=1)

    record = after
    record["hard_exp"] = time.time() + 7200
    await ur._store.put(handle, record, collection=ur._COLLECTION)
    await client.put(
        f"/ul/{handle}", content=b"x", headers={"Content-Range": "bytes 1-1/10"}
    )
    assert (await _record(handle))["exp"] == pytest.approx(
        time.time() + ur._IDLE_SECONDS, abs=5
    )


# --- logging -------------------------------------------------------------------------


@pytest.fixture
def entrypoint_http_log_levels():
    """main.py and fastmcp_server.py hold these at WARNING: at INFO httpx logs
    every request URL, and the session URL is a write capability."""
    names = ("httpx", "httpcore", "httpx2", "httpcore2")
    saved = {name: logging.getLogger(name).level for name in names}
    for name in names:
        logging.getLogger(name).setLevel(logging.WARNING)
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("entrypoint", ["main.py", "fastmcp_server.py"])
def test_entrypoints_keep_httpx_request_logs_off(entrypoint):
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / entrypoint).read_text()
    assert 'for _http_logger in ("httpx", "httpcore", "httpx2", "httpcore2"):' in source
    assert "logging.getLogger(_http_logger).setLevel(logging.WARNING)" in source


@pytest.mark.asyncio
async def test_no_log_record_carries_the_handle_or_session(
    client, google, caplog, entrypoint_http_log_levels
):
    google.reply = lambda request, body: httpx.Response(404)
    with caplog.at_level(logging.DEBUG):
        handle = await _handle()
        await client.put(f"/ul/{handle}", content=b"secret file bytes")
        await client.put(f"/ul/{handle}", content=b"secret file bytes")
    text = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    assert handle not in text
    assert "ABCdef123_secret" not in text and "upload_id" not in text
    assert "secret file bytes" not in text


# --- flag off ------------------------------------------------------------------------


def test_flag_off_mounts_no_route(monkeypatch):
    monkeypatch.delenv(ur.FLAG_ENV, raising=False)
    before = list(server._additional_http_routes)
    core_server._upload_relay_route_registered = False
    try:
        core_server._ensure_upload_relay_route()
        assert server._additional_http_routes == before
        assert all(
            getattr(r, "path", "") != ur.ROUTE_PATH
            for r in server.http_app().router.routes
        )
    finally:
        server._additional_http_routes[:] = before
        core_server._upload_relay_route_registered = False


def test_flag_on_mounts_the_route_once(relay_env):
    before = list(server._additional_http_routes)
    core_server._upload_relay_route_registered = False
    try:
        core_server._ensure_upload_relay_route()
        core_server._ensure_upload_relay_route()
        added = server._additional_http_routes[len(before) :]
        assert [r.path for r in added] == [ur.ROUTE_PATH]
        assert added[0].methods >= {"PUT", "POST"}
    finally:
        server._additional_http_routes[:] = before
        core_server._upload_relay_route_registered = False


@pytest.mark.asyncio
async def test_flag_off_ul_answers_as_an_unknown_path(monkeypatch):
    monkeypatch.delenv(ur.FLAG_ENV, raising=False)
    before = list(server._additional_http_routes)
    core_server._upload_relay_route_registered = False
    core_server._ensure_upload_relay_route()
    assert server._additional_http_routes == before
    transport = httpx.ASGITransport(app=server.http_app())
    async with httpx.AsyncClient(transport=transport, base_url=EXTERNAL) as http:
        ul = await http.put("/ul/" + "A" * 22, content=b"x")
        other = await http.put("/no-such-route/" + "A" * 22, content=b"x")
    assert (ul.status_code, ul.content) == (other.status_code, other.content)
    assert ul.status_code == 404


def test_flag_off_leaves_tool_docs_alone(monkeypatch):
    monkeypatch.delenv(ur.FLAG_ENV, raising=False)

    def tool():
        """return_upload_url (bool): Return a Google resumable-upload session URL."""

    original = tool.__doc__
    assert ur.describe(tool) is tool and tool.__doc__ == original


@pytest.mark.asyncio
async def test_inert_if_reached_while_off(relay_env, monkeypatch, google):
    handle = await _handle()
    monkeypatch.setenv(ur.FLAG_ENV, "false")
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "PUT",
        "path": f"/ul/{handle}",
        "headers": [(b"content-length", b"1")],
        "path_params": {"handle": handle},
        "query_string": b"",
    }
    response = await ur.serve(Request(scope))
    assert response.status_code == 404
    assert google.requests == []


# --- tool-side offer ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_offer_returns_a_link_on_this_server(relay_env):
    text = await ur.offer_result("Summary.", GURL, "application/pdf", USER)
    assert text.startswith("Summary.")
    assert f"{EXTERNAL}/ul/" in text
    assert "googleapis.com" not in text and "upload_id" not in text
    assert "100 MiB" in text and "15 minutes" in text and "2 hours" in text
    assert "--fail-with-body" in text and "Content-Type: application/pdf" in text
    assert "at most 50 requests" in text
    assert "if its body says the upload completed, do not upload again" in text
    assert "the same query returns the file's metadata again" in text
    handle = text.split("/ul/")[1].split()[0].strip("'")
    assert (await ur._load(handle))["mt"] == "application/pdf"


@pytest.mark.asyncio
async def test_offer_fails_loud_without_signed_downloads(relay_env, monkeypatch):
    monkeypatch.delenv(sd.FLAG_ENV, raising=False)
    text = await ur.offer_result("Summary.", GURL, "application/pdf", USER)
    assert text.startswith("Error:")
    assert "googleapis.com" not in text


@pytest.mark.asyncio
async def test_offer_fails_loud_without_an_external_url(relay_env, monkeypatch):
    monkeypatch.delenv("WORKSPACE_EXTERNAL_URL", raising=False)
    text = await ur.offer_result("Summary.", GURL, "application/pdf", USER)
    assert text.startswith("Error:") and "WORKSPACE_EXTERNAL_URL" in text


@pytest.mark.asyncio
async def test_offer_fails_loud_without_key_material(relay_env, monkeypatch):
    with patch.object(sd, "_signing_key", side_effect=RuntimeError("no key")):
        text = await ur.offer_result("Summary.", GURL, "application/pdf", USER)
    assert text.startswith("Error:") and "key" in text


@pytest.mark.asyncio
async def test_offer_fails_loud_on_an_unexpected_session_url(relay_env):
    text = await ur.offer_result(
        "Summary.", "https://evil.example/upload?upload_id=X", "a/b", USER
    )
    assert text.startswith("Error:") and "evil" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "creds",
    [
        None,  # nothing stored for the user, e.g. a service-account deployment
        Credentials(token=None),  # no access token and no refresh token
    ],
    ids=["unrecoverable", "unusable"],
)
async def test_offer_fails_loud_when_the_route_could_not_serve_the_owner(
    relay_env, monkeypatch, creds
):
    """Parity with /dl/: offer_url mints nothing the route would answer 401."""
    monkeypatch.setattr(sd, "_recover_credentials", Mock(return_value=creds))
    store = AsyncMock()
    monkeypatch.setattr(ur, "store_session", store)
    text = await ur.offer_result("Summary.", GURL, "application/pdf", USER)
    assert text.startswith("Error: no upload URL could be issued")
    assert "credentials" in text
    assert "googleapis.com" not in text and "/ul/" not in text
    store.assert_not_called()
    sd._recover_credentials.assert_called_once_with(USER)


@pytest.mark.asyncio
async def test_offer_fails_loud_when_the_store_is_down(relay_env, monkeypatch):
    monkeypatch.setattr(ur._store, "put", AsyncMock(side_effect=RuntimeError("down")))
    text = await ur.offer_result("Summary.", GURL, "a/b", USER)
    assert text.startswith("Error:") and "googleapis.com" not in text


def test_error_text_is_one_line_and_names_no_google_url():
    text = ur._error_text("reason")
    assert "\n" not in text and "googleapis" not in text


def test_json_bodies_are_json():
    response = ur._error(410, "closed")
    assert json.loads(response.body) == {"error": "closed"}
