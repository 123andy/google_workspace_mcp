"""Tests for Gmail body_format support across helper and public tool APIs."""

import base64
from pathlib import Path
from unittest.mock import Mock

import pytest

import gmail.gmail_tools as gmail_tools
from core.utils import UserInputError
from gmail.gmail_tools import (
    _extract_message_bodies,
    _format_body_content,
    get_gmail_message_content,
    get_gmail_message_full,
    get_gmail_messages_content_batch,
    get_gmail_thread_content,
    get_gmail_threads_content_batch,
)


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _headers(**overrides):
    header_map = {
        "Subject": "Example subject",
        "From": "sender@example.com",
        "To": "recipient@example.com",
        "Cc": "cc@example.com",
        "Message-ID": "<message@example.com>",
        "Date": "Fri, 28 Mar 2026 10:00:00 -0400",
    }
    header_map.update(overrides)
    return [{"name": name, "value": value} for name, value in header_map.items()]


def _payload(headers=None, text=None, html=None):
    payload = {"headers": headers or _headers()}
    parts = []
    if text is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _encode(text)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _encode(html)}})
    if parts:
        payload["mimeType"] = "multipart/alternative"
        payload["parts"] = parts
    return payload


def _message_response(message_id: str, text="", html="", headers=None):
    return {
        "id": message_id,
        "payload": _payload(headers=headers, text=text, html=html),
    }


def _metadata_response(message_id: str, headers=None):
    return {
        "id": message_id,
        "payload": {"headers": headers or _headers()},
    }


def _thread_message(message_id: str, text="", html="", headers=None):
    return {
        "id": message_id,
        "payload": _payload(headers=headers, text=text, html=html),
    }


class _FakeBatch:
    def __init__(self, callback):
        self._callback = callback
        self._requests = []

    def add(self, request, request_id):
        self._requests.append((request_id, request))

    def execute(self):
        for request_id, request in self._requests:
            try:
                response = request.execute()
                self._callback(request_id, response, None)
            except Exception as exc:
                self._callback(request_id, None, exc)


def _build_service(*, message_responses=None, thread_responses=None):
    message_responses = message_responses or {}
    thread_responses = thread_responses or {}

    service = Mock()

    def message_get(**kwargs):
        request = Mock()
        response = message_responses[(kwargs["id"], kwargs["format"])]
        if isinstance(response, Exception):
            request.execute.side_effect = response
        else:
            request.execute.return_value = response
        return request

    def thread_get(**kwargs):
        request = Mock()
        response = thread_responses[(kwargs["id"], kwargs["format"])]
        if isinstance(response, Exception):
            request.execute.side_effect = response
        else:
            request.execute.return_value = response
        return request

    service.users().messages().get.side_effect = message_get
    service.users().threads().get.side_effect = thread_get
    service.new_batch_http_request.side_effect = lambda callback: _FakeBatch(callback)
    return service


class TestFormatBodyContentTextMode:
    """Verify default 'text' body_format preserves existing behavior."""

    def test_returns_text_body_when_available(self):
        result = _format_body_content("Hello world", "<b>Hello world</b>")
        assert result == "Hello world"

    def test_returns_text_body_default_format(self):
        result = _format_body_content(
            "Hello world", "<b>Hello world</b>", body_format="text"
        )
        assert result == "Hello world"

    def test_falls_back_to_html_when_text_empty(self):
        result = _format_body_content("", "<p>HTML content here</p>")
        assert "HTML content here" in result

    def test_returns_no_content_when_both_empty(self):
        result = _format_body_content("", "")
        assert result == "[No readable content found]"

    def test_detects_low_value_placeholder_text(self):
        low_value = "Your client does not support HTML messages"
        html = "<p>This is the actual email content with much more detail</p>"
        result = _format_body_content(low_value, html)
        assert "actual email content" in result

    def test_truncates_long_html_fallback(self):
        long_html = "<p>" + "x" * 25000 + "</p>"
        result = _format_body_content("", long_html)
        assert "[Content truncated...]" in result


class TestFormatBodyContentHtmlMode:
    """Verify 'html' body_format returns raw HTML."""

    def test_returns_raw_html_body(self):
        html = "<div><b>Hello</b> <em>world</em></div>"
        result = _format_body_content("Hello world", html, body_format="html")
        assert result == html

    def test_returns_html_without_conversion(self):
        html = "<table><tr><td>Cell</td></tr></table>"
        result = _format_body_content("Cell", html, body_format="html")
        assert "<table>" in result
        assert "<td>Cell</td>" in result

    def test_falls_back_to_text_when_no_html(self):
        result = _format_body_content("Plain text only", "", body_format="html")
        assert result == "Plain text only"

    def test_returns_no_content_when_both_empty(self):
        result = _format_body_content("", "", body_format="html")
        assert result == "[No readable content found]"

    def test_strips_whitespace_from_html(self):
        result = _format_body_content("text", "  <b>html</b>  ", body_format="html")
        assert result == "<b>html</b>"

    def test_truncates_long_html(self):
        long_html = "<div>" + "x" * 25000 + "</div>"
        result = _format_body_content("text", long_html, body_format="html")
        assert "[Content truncated...]" in result
        assert len(result) < len(long_html)

    def test_preserves_html_entities(self):
        html = "<p>Price: &lt;$100 &amp; free shipping</p>"
        result = _format_body_content("", html, body_format="html")
        assert "&lt;" in result
        assert "&amp;" in result

    def test_preserves_style_and_script_tags(self):
        html = "<style>body { color: red; }</style><p>Content</p>"
        result = _format_body_content("Content", html, body_format="html")
        assert "<style>" in result
        assert "color: red" in result

    def test_whitespace_only_html_falls_back_to_text(self):
        result = _format_body_content("Fallback text", "   \n\t  ", body_format="html")
        assert result == "Fallback text"


class TestExtractMessageBodies:
    """Verify _extract_message_bodies extracts both text and HTML parts."""

    def test_extracts_text_and_html_from_multipart(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encode("Plain text")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _encode("<b>HTML</b>")},
                },
            ],
        }
        bodies = _extract_message_bodies(payload)
        assert bodies["text"] == "Plain text"
        assert bodies["html"] == "<b>HTML</b>"

    def test_extracts_text_only(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": _encode("Just text")},
        }
        bodies = _extract_message_bodies(payload)
        assert bodies["text"] == "Just text"
        assert bodies["html"] == ""

    def test_extracts_html_only(self):
        payload = {
            "mimeType": "text/html",
            "body": {"data": _encode("<p>Just HTML</p>")},
        }
        bodies = _extract_message_bodies(payload)
        assert bodies["text"] == ""
        assert bodies["html"] == "<p>Just HTML</p>"

    def test_handles_empty_payload(self):
        bodies = _extract_message_bodies({})
        assert bodies["text"] == ""
        assert bodies["html"] == ""

    def test_handles_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _encode("Nested text")},
                        },
                        {
                            "mimeType": "text/html",
                            "body": {"data": _encode("<p>Nested HTML</p>")},
                        },
                    ],
                },
            ],
        }
        bodies = _extract_message_bodies(payload)
        assert bodies["text"] == "Nested text"
        assert bodies["html"] == "<p>Nested HTML</p>"


@pytest.mark.asyncio
async def test_get_gmail_message_content_returns_raw_mime():
    service = _build_service(
        message_responses={
            ("msg-1", "metadata"): _metadata_response("msg-1"),
            ("msg-1", "raw"): {"raw": _encode("Raw MIME body")},
        }
    )

    result = await _unwrap(get_gmail_message_content)(
        service=service,
        message_id="msg-1",
        user_google_email="user@example.com",
        body_format="raw",
    )

    assert "--- RAW MIME ---" in result
    assert "Raw MIME body" in result
    assert "From: sender@example.com" in result
    assert "Date: Fri, 28 Mar 2026 10:00:00 -0400" in result
    assert "To: recipient@example.com" in result
    assert "Cc: cc@example.com" in result
    assert "From:    " not in result


@pytest.mark.asyncio
async def test_get_gmail_message_content_reports_raw_decode_errors():
    service = _build_service(
        message_responses={
            ("msg-1", "metadata"): _metadata_response("msg-1"),
            ("msg-1", "raw"): {"raw": "a"},
        }
    )

    result = await _unwrap(get_gmail_message_content)(
        service=service,
        message_id="msg-1",
        user_google_email="user@example.com",
        body_format="raw",
    )

    assert "[Failed to decode raw MIME:" in result


@pytest.mark.asyncio
async def test_get_gmail_message_content_truncates_raw_mime(monkeypatch):
    monkeypatch.setattr(gmail_tools, "RAW_BODY_TRUNCATE_LIMIT", 12)
    service = _build_service(
        message_responses={
            ("msg-1", "metadata"): _metadata_response("msg-1"),
            ("msg-1", "raw"): {"raw": _encode("x" * 32)},
        }
    )

    result = await _unwrap(get_gmail_message_content)(
        service=service,
        message_id="msg-1",
        user_google_email="user@example.com",
        body_format="raw",
    )

    assert "--- RAW MIME ---" in result
    assert "[Content truncated...]" in result


@pytest.mark.asyncio
async def test_get_gmail_messages_content_batch_supports_raw_format():
    service = _build_service(
        message_responses={
            ("msg-1", "metadata"): _metadata_response("msg-1"),
            ("msg-1", "raw"): {"raw": _encode("Batch raw MIME body")},
        }
    )

    result = await _unwrap(get_gmail_messages_content_batch)(
        service=service,
        message_ids=["msg-1"],
        user_google_email="user@example.com",
        body_format="raw",
    )

    assert "Retrieved 1 messages" in result
    assert "--- RAW MIME ---" in result
    assert "Batch raw MIME body" in result

    formats = [
        call.kwargs["format"]
        for call in service.users.return_value.messages.return_value.get.call_args_list
    ]
    assert formats.count("metadata") == 1
    assert formats.count("raw") == 1


@pytest.mark.asyncio
async def test_get_gmail_messages_content_batch_default_text_format():
    service = _build_service(
        message_responses={
            ("msg-1", "full"): _message_response(
                "msg-1", text="Plain text body", html="<p>HTML body</p>"
            ),
        }
    )

    result = await _unwrap(get_gmail_messages_content_batch)(
        service=service,
        message_ids=["msg-1"],
        user_google_email="user@example.com",
    )

    assert "Plain text body" in result
    assert "--- BODY ---" in result
    assert "--- RAW MIME ---" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("body_format", ["html", "raw"])
async def test_get_gmail_messages_content_batch_rejects_metadata_with_body_format(
    body_format,
):
    service = _build_service(
        message_responses={
            ("msg-1", "metadata"): _metadata_response("msg-1"),
        }
    )

    with pytest.raises(UserInputError, match="require format='full'"):
        await _unwrap(get_gmail_messages_content_batch)(
            service=service,
            message_ids=["msg-1"],
            user_google_email="user@example.com",
            format="metadata",
            body_format=body_format,
        )


@pytest.mark.asyncio
async def test_get_gmail_thread_content_supports_raw_format():
    service = _build_service(
        message_responses={
            ("msg-1", "raw"): {"raw": _encode("Thread raw MIME 1")},
            ("msg-2", "raw"): {"raw": _encode("Thread raw MIME 2")},
        },
        thread_responses={
            (
                "thread-1",
                "full",
            ): {
                "messages": [
                    _thread_message("msg-1", text="Plain 1", html="<p>HTML 1</p>"),
                    _thread_message("msg-2", text="Plain 2", html="<p>HTML 2</p>"),
                ]
            }
        },
    )

    result = await _unwrap(get_gmail_thread_content)(
        service=service,
        thread_id="thread-1",
        user_google_email="user@example.com",
        body_format="raw",
    )

    assert result.count("--- RAW MIME ---") == 2
    assert "Thread raw MIME 1" in result
    assert "Thread raw MIME 2" in result


@pytest.mark.asyncio
async def test_get_gmail_threads_content_batch_supports_raw_format():
    service = _build_service(
        message_responses={
            ("msg-1", "raw"): {"raw": _encode("Batch thread raw MIME")},
        },
        thread_responses={
            (
                "thread-1",
                "full",
            ): {
                "messages": [
                    _thread_message("msg-1", text="Plain 1", html="<p>HTML 1</p>")
                ]
            }
        },
    )

    result = await _unwrap(get_gmail_threads_content_batch)(
        service=service,
        thread_ids=["thread-1"],
        user_google_email="user@example.com",
        body_format="raw",
    )

    assert "Retrieved 1 threads:" in result
    assert "--- RAW MIME ---" in result
    assert "Batch thread raw MIME" in result


@pytest.mark.asyncio
async def test_get_gmail_message_content_preserves_html_format():
    service = _build_service(
        message_responses={
            ("msg-1", "metadata"): _metadata_response("msg-1"),
            (
                "msg-1",
                "full",
            ): _message_response(
                "msg-1", text="Plain fallback", html="<p><b>HTML</b></p>"
            ),
        }
    )

    result = await _unwrap(get_gmail_message_content)(
        service=service,
        message_id="msg-1",
        user_google_email="user@example.com",
        body_format="html",
    )

    assert "<p><b>HTML</b></p>" in result
    assert "From: sender@example.com" in result
    assert "Date: Fri, 28 Mar 2026 10:00:00 -0400" in result
    assert "To: recipient@example.com" in result
    assert "Cc: cc@example.com" in result
    assert "From:    " not in result


@pytest.fixture
def stdio_storage(monkeypatch, tmp_path):
    """Route attachment storage to a temp dir and force stdio (file-path) delivery."""
    import core.attachment_storage as attachment_storage

    monkeypatch.setattr(attachment_storage, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(attachment_storage, "_attachment_storage", None)
    monkeypatch.setattr("core.config.get_transport_mode", lambda: "stdio")
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: False)
    monkeypatch.setattr(
        "core.attachment_signing.signed_attachment_urls_enabled", lambda: False
    )
    return tmp_path


def _saved_path(result: str) -> str:
    """Pull the on-disk path out of a stdio-mode get_gmail_message_full response."""
    marker = "📎 Saved to: "
    line = next(ln for ln in result.splitlines() if marker in ln)
    return line.split(marker, 1)[1].strip()


def _unpadded(text: str) -> str:
    """base64url without padding, exactly as the Gmail API returns raw content."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.mark.asyncio
async def test_get_gmail_message_full_eml_saves_complete_raw(stdio_storage):
    # Use unpadded base64url (as Gmail actually returns) to exercise padding repair.
    raw_mime = "From: sender@example.com\r\n\r\nComplete raw MIME body!"
    service = _build_service(
        message_responses={
            ("msg-1", "metadata"): _metadata_response("msg-1"),
            ("msg-1", "raw"): {"raw": _unpadded(raw_mime)},
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-1",
        user_google_email="user@example.com",
        deliver_as="eml",
    )

    # Body content must NOT be inlined in the response.
    assert "Complete raw MIME body" not in result
    assert "--- FULL MESSAGE EXPORT ---" in result
    assert "Format: eml" in result
    assert "Subject: Example subject" in result

    # The saved file must hold the complete, decoded message.
    with open(_saved_path(result), "rb") as fh:
        assert fh.read().decode() == raw_mime


@pytest.mark.asyncio
async def test_get_gmail_message_full_txt_uses_plaintext(stdio_storage):
    service = _build_service(
        message_responses={
            ("msg-2", "metadata"): _metadata_response("msg-2"),
            ("msg-2", "full"): _message_response(
                "msg-2", text="Plain body", html="<b>Plain body</b>"
            ),
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-2",
        user_google_email="user@example.com",
        deliver_as="txt",
    )

    assert "Plain body" not in result
    assert "Format: txt" in result
    with open(_saved_path(result), "rb") as fh:
        assert fh.read().decode() == "Plain body"


@pytest.mark.asyncio
async def test_get_gmail_message_full_html_saves_raw_html(stdio_storage):
    service = _build_service(
        message_responses={
            ("msg-3", "metadata"): _metadata_response("msg-3"),
            ("msg-3", "full"): _message_response(
                "msg-3", text="fallback", html="<p><b>Rich HTML</b></p>"
            ),
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-3",
        user_google_email="user@example.com",
        deliver_as="html",
    )

    assert "Rich HTML" not in result
    with open(_saved_path(result), "rb") as fh:
        assert fh.read().decode() == "<p><b>Rich HTML</b></p>"


@pytest.mark.asyncio
async def test_get_gmail_message_full_preserves_edge_whitespace(stdio_storage):
    """A complete export must not strip leading/trailing body content."""
    html_with_edges = "\n\n  <html><body>Body</body></html>  \n"
    text_with_edges = "\n   Leading and trailing kept.   \n"
    service = _build_service(
        message_responses={
            ("msg-ws", "metadata"): _metadata_response("msg-ws"),
            ("msg-ws", "full"): _message_response(
                "msg-ws", text=text_with_edges, html=html_with_edges
            ),
        }
    )

    html_result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-ws",
        user_google_email="user@example.com",
        deliver_as="html",
    )
    with open(_saved_path(html_result), "rb") as fh:
        assert fh.read().decode() == html_with_edges

    txt_result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-ws",
        user_google_email="user@example.com",
        deliver_as="txt",
    )
    with open(_saved_path(txt_result), "rb") as fh:
        assert fh.read().decode() == text_with_edges


@pytest.mark.asyncio
async def test_get_gmail_message_full_does_not_truncate(stdio_storage):
    """The whole point: full-body export never applies the 20,000-char cap."""
    long_html = "<p>" + ("A" * 50000) + "</p>"
    service = _build_service(
        message_responses={
            ("msg-4", "metadata"): _metadata_response("msg-4"),
            ("msg-4", "full"): _message_response("msg-4", text="", html=long_html),
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-4",
        user_google_email="user@example.com",
        deliver_as="html",
    )

    assert "[Content truncated...]" not in result
    with open(_saved_path(result), "rb") as fh:
        saved = fh.read().decode()
    assert saved == long_html
    assert len(saved) > 20000


@pytest.mark.asyncio
async def test_get_gmail_message_full_http_returns_url(monkeypatch, tmp_path):
    import core.attachment_storage as attachment_storage

    monkeypatch.setattr(attachment_storage, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(attachment_storage, "_attachment_storage", None)
    monkeypatch.setattr("core.config.get_transport_mode", lambda: "streamable-http")
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: False)
    monkeypatch.setattr(
        "core.attachment_signing.signed_attachment_urls_enabled", lambda: False
    )
    monkeypatch.setattr(
        attachment_storage,
        "get_attachment_url",
        lambda file_id: f"https://example.test/attachments/{file_id}",
    )

    service = _build_service(
        message_responses={
            ("msg-5", "metadata"): _metadata_response("msg-5"),
            ("msg-5", "raw"): {"raw": _encode("raw body")},
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-5",
        user_google_email="user@example.com",
        deliver_as="eml",
    )

    assert "https://example.test/attachments/" in result
    assert "raw body" not in result


@pytest.mark.asyncio
async def test_get_gmail_message_full_blocked_in_stateless_without_signed(monkeypatch):
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: True)
    monkeypatch.setattr(
        "core.attachment_signing.signed_attachment_urls_enabled", lambda: False
    )
    service = _build_service(
        message_responses={("msg-6", "metadata"): _metadata_response("msg-6")}
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-6",
        user_google_email="user@example.com",
        deliver_as="eml",
    )

    assert "stateless mode" in result.lower()
    assert "get_gmail_message_content" in result


@pytest.mark.asyncio
async def test_get_gmail_message_full_returns_signed_url(monkeypatch):
    """When signed URLs are enabled, the tool returns a signed URL and never fetches
    or streams the body itself (works under stateless mode)."""
    import auth.oauth21_session_store as sess
    import core.attachment_cred_cache as cred_cache
    import core.attachment_signing as signing

    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: True)
    monkeypatch.setattr(signing, "signed_attachment_urls_enabled", lambda: True)
    monkeypatch.setattr(signing, "clamp_ttl_to_expiry", lambda expiry: 600)

    captured = {}

    async def fake_build_download_url(**kwargs):
        captured.update(kwargs)
        return "https://example.test/attachments/signed/TOKEN123"

    monkeypatch.setattr(signing, "build_download_url", fake_build_download_url)

    async def fake_stash(*args, **kwargs):
        return None

    monkeypatch.setattr(cred_cache, "stash_credentials", fake_stash)

    creds = Mock()
    creds.expiry = "future"
    store = Mock()
    store.get_credentials.return_value = creds
    monkeypatch.setattr(sess, "get_oauth21_session_store", lambda: store)

    # Only metadata is fetched; a raw/full body fetch would KeyError this service.
    service = _build_service(
        message_responses={("msg-sign", "metadata"): _metadata_response("msg-sign")}
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-sign",
        user_google_email="user@example.com",
        deliver_as="eml",
    )

    assert "https://example.test/attachments/signed/TOKEN123" in result
    assert "signed URL" in result
    assert captured["source"] == "gmail_message"
    assert captured["ref"] == {"mid": "msg-sign", "fmt": "eml"}


@pytest.mark.asyncio
async def test_fetch_gmail_message_eml_decodes_raw(monkeypatch):
    import core.signed_download as sd

    raw = _unpadded("From: a@b\r\n\r\nfull raw message")
    fake = Mock()
    fake.users().messages().get().execute.return_value = {"raw": raw}
    monkeypatch.setattr(sd, "build", lambda *a, **k: fake)

    res = await sd._fetch_gmail_message(
        {"mid": "m", "fmt": "eml", "fn": "x.eml", "mt": "message/rfc822"}, Mock()
    )

    assert res.content == b"From: a@b\r\n\r\nfull raw message"
    assert res.filename == "x.eml"
    assert res.media_type == "message/rfc822"


@pytest.mark.asyncio
async def test_fetch_gmail_message_html_extracts_body(monkeypatch):
    import core.signed_download as sd

    fake = Mock()
    fake.users().messages().get().execute.return_value = {
        "payload": _payload(text="", html="<p>Rich body</p>")
    }
    monkeypatch.setattr(sd, "build", lambda *a, **k: fake)

    res = await sd._fetch_gmail_message({"mid": "m", "fmt": "html"}, Mock())

    assert res.content.decode() == "<p>Rich body</p>"
    assert res.media_type == "text/html"


@pytest.mark.asyncio
async def test_get_gmail_message_full_empty_raw_errors(stdio_storage):
    service = _build_service(
        message_responses={
            ("msg-7", "metadata"): _metadata_response("msg-7"),
            ("msg-7", "raw"): {"raw": ""},
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-7",
        user_google_email="user@example.com",
        deliver_as="eml",
    )

    assert result.startswith("Error:")
    assert "no raw content" in result


@pytest.mark.asyncio
async def test_get_gmail_message_full_no_body_errors(stdio_storage):
    service = _build_service(
        message_responses={
            ("msg-8", "metadata"): _metadata_response("msg-8"),
            ("msg-8", "full"): _message_response("msg-8", text="", html=""),
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-8",
        user_google_email="user@example.com",
        deliver_as="txt",
    )

    assert result.startswith("Error:")
    assert "no readable body" in result


@pytest.mark.asyncio
async def test_get_gmail_message_full_txt_converts_html_when_no_plaintext(stdio_storage):
    service = _build_service(
        message_responses={
            ("msg-9", "metadata"): _metadata_response("msg-9"),
            ("msg-9", "full"): _message_response(
                "msg-9", text="", html="<p>Converted body</p>"
            ),
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-9",
        user_google_email="user@example.com",
        deliver_as="txt",
    )

    with open(_saved_path(result), "rb") as fh:
        assert fh.read().decode() == "Converted body"


@pytest.mark.asyncio
async def test_get_gmail_message_full_html_falls_back_to_text_and_labels_it(
    stdio_storage,
):
    service = _build_service(
        message_responses={
            ("msg-10", "metadata"): _metadata_response("msg-10"),
            ("msg-10", "full"): _message_response(
                "msg-10", text="Only plaintext here", html=""
            ),
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-10",
        user_google_email="user@example.com",
        deliver_as="html",
    )

    assert "No HTML body present" in result
    saved_path = _saved_path(result)
    assert saved_path.endswith(".txt")
    with open(saved_path, "rb") as fh:
        assert fh.read().decode() == "Only plaintext here"


@pytest.mark.asyncio
async def test_get_gmail_message_full_sanitizes_subject_filename(stdio_storage):
    headers = _headers(Subject="../../etc/passwd")
    service = _build_service(
        message_responses={
            ("msg-11", "metadata"): _metadata_response("msg-11", headers=headers),
            ("msg-11", "raw"): {"raw": _encode("raw body")},
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-11",
        user_google_email="user@example.com",
        deliver_as="eml",
    )

    saved_path = _saved_path(result)
    # No path traversal escapes the storage dir.
    assert Path(saved_path).parent == Path(stdio_storage)
    assert "/" not in Path(saved_path).name.replace(str(stdio_storage), "")


@pytest.mark.asyncio
async def test_get_gmail_message_full_long_subject_does_not_crash(stdio_storage):
    headers = _headers(Subject="X" * 900)
    service = _build_service(
        message_responses={
            ("msg-12", "metadata"): _metadata_response("msg-12", headers=headers),
            ("msg-12", "raw"): {"raw": _encode("raw body")},
        }
    )

    result = await _unwrap(get_gmail_message_full)(
        service=service,
        message_id="msg-12",
        user_google_email="user@example.com",
        deliver_as="eml",
    )

    saved_path = _saved_path(result)
    assert Path(saved_path).exists()
    assert len(Path(saved_path).name) < 255
