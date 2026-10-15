"""Signed upload links: ``/ul/{handle}`` relays a PUT to a Google Drive
resumable upload session (this fork).

``return_upload_url`` normally returns Google's own session URL, so the caller
must be able to reach ``www.googleapis.com``. A sandboxed client may only be
allowed to reach this server. With ``WORKSPACE_MCP_SIGNED_UPLOAD_URLS=true``
the tools open the same session but return ``{external_base}/ul/{handle}``,
and this route streams the caller's bytes to the session. The session URL
never leaves the server.

The handle is a random 128-bit capability, as for ``/dl/{handle}``
(:mod:`core.download_handles`). The record behind it (session URL, owner,
MIME type, limits) sits in the shared KV store, Fernet-encrypted under its own
salt. The session URL is validated when stored and again when loaded, so the
relay can only ever reach Google's upload endpoint.

Per link: one request at a time (per process); at most ``_MAX_REQUESTS``
requests and twice the size cap in total; first use within
``_FIRST_USE_SECONDS``, then expiry slides with use up to
``_MAX_LIFETIME_SECONDS`` after minting. A completed upload or a Google 4xx
closes the link (410); a status query on a completed link replays Google's
reply. The relay answers only after Google has, so an upload that fails partway
never looks like a success.

Off by default. Off, the route is not mounted and every tool behaves exactly as
without this module. Needs signed download links on (their external URL and
key checks) and streamable-http; stdio ignores the setting.
"""

import asyncio
import logging
import os
import re
import secrets
import time
from typing import Optional

import httpx
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect, Request

from core import signed_downloads
from core.config import get_transport_mode

logger = logging.getLogger(__name__)

FLAG_ENV = "WORKSPACE_MCP_SIGNED_UPLOAD_URLS"
MAX_BYTES_ENV = "WORKSPACE_MCP_SIGNED_UPLOAD_MAX_BYTES"
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
ROUTE_PATH = "/ul/{handle}"

_COLLECTION = "signed_upload_sessions"
_HANDLE_BYTES = 16
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

_FIRST_USE_SECONDS = 15 * 60
_IDLE_SECONDS = 15 * 60
_MAX_LIFETIME_SECONDS = 2 * 60 * 60
_MAX_REQUESTS = 50
# Google answers with small JSON (file metadata or an error); never more.
_MAX_RESPONSE_BYTES = 64 * 1024
# A client that stops sending for this long is treated as gone.
_CLIENT_IDLE_SECONDS = 60.0
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=120.0, pool=30.0)
_RESPONSE_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}

_CONTENT_RANGE_RE = re.compile(r"^bytes (?:(\d{1,15})-(\d{1,15})|\*)/(\d{1,15}|\*)$")
_RANGE_RE = re.compile(r"^bytes=\d{1,15}-\d{1,15}$")

# Handles with a relay in progress in this process.
_in_flight: set[str] = set()

_store = None
_store_built = False


def flag_set() -> bool:
    # Parsed exactly like the signed-download flag it depends on.
    return os.getenv(FLAG_ENV, "false").lower() == "true"


def active() -> bool:
    """On, and over streamable-http (stdio has no route to serve the link)."""
    return flag_set() and get_transport_mode() == "streamable-http"


def _parse_max_bytes(raw: Optional[str]) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_MAX_BYTES
    value = raw.strip()
    if not (value.isascii() and value.isdigit()) or int(value) <= 0:
        raise ValueError(
            f"{MAX_BYTES_ENV} must be a positive number of bytes, got {raw!r}"
        )
    return int(value)


def max_bytes() -> int:
    try:
        return _parse_max_bytes(os.getenv(MAX_BYTES_ENV))
    except ValueError:
        return DEFAULT_MAX_BYTES


def log_if_ignored(transport: str) -> None:
    if flag_set() and transport != "streamable-http":
        logger.warning(
            "%s is set but the transport is %s; upload links on this server are "
            "only issued over streamable-http, so the setting is ignored.",
            FLAG_ENV,
            transport,
        )


def validate_startup(transport: str) -> None:
    """Raise ``ValueError`` when the setting is on over streamable-http but no
    link could work. Off or stdio: no check, no log."""
    if not flag_set() or transport != "streamable-http":
        return
    problems = []
    if not signed_downloads._flag_set():
        problems.append(
            f"{FLAG_ENV}=true requires {signed_downloads.FLAG_ENV}=true (the upload "
            "links use its external URL and key checks)."
        )
    try:
        cap = _parse_max_bytes(os.getenv(MAX_BYTES_ENV))
    except ValueError as exc:
        problems.append(str(exc))
        cap = DEFAULT_MAX_BYTES
    if problems:
        raise ValueError("\n".join(problems))
    logger.info(
        "%s is on: upload links use %s/ul/*, which must be publicly reachable and "
        "accept request bodies up to %d bytes.",
        FLAG_ENV,
        signed_downloads._base_url(),
        cap,
    )


# --- Session URL validation ----------------------------------------------------------


def _allowed_origin() -> tuple[str, str, int]:
    """Where a stored session URL may point. Tests substitute a local fake by
    patching this function; no setting can change it."""
    return "https", "www.googleapis.com", 443


def is_valid_session_url(url: object) -> bool:
    """True only for a Drive resumable session URL on Google's upload host:
    exact scheme and host, default port, no userinfo, no fragment, ASCII with
    no whitespace or backslash, upload path, and an ``upload_id``."""
    if not isinstance(url, str) or not url or len(url) > 4096:
        return False
    if not url.isascii() or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    if "\\" in url:
        return False
    scheme, host, port = _allowed_origin()
    # Checked on the raw string too, so nothing a parser might normalise away
    # (case, a trailing dot, userinfo, an explicit port) can slip through.
    if not (
        url.startswith(f"{scheme}://{host}/")
        or url.startswith(f"{scheme}://{host}:{port}/")
    ):
        return False
    try:
        parsed = httpx.URL(url)
    except Exception:
        return False
    if (
        parsed.scheme != scheme
        or parsed.host != host
        or parsed.port not in (None, port)
        or parsed.userinfo
        or parsed.fragment
    ):
        return False
    path = parsed.path
    if not path.startswith("/upload/drive/v3/files") or ".." in path:
        return False
    return bool(parsed.params.get("upload_id"))


# --- Handle store --------------------------------------------------------------------


def _valid_mime_type(value: object) -> bool:
    """A header-safe MIME type: printable ASCII, no spaces at the ends."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 255
        and value.isascii()
        and value.isprintable()
        and value == value.strip()
    )


def _build_store():
    """Encrypted shared backend when configured, else in-process memory."""
    global _store, _store_built
    if _store_built:
        return _store
    _store_built = True
    try:
        from cryptography.fernet import Fernet
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

        from core.storage import derive_shared_fernet_key, get_configured_kv_store

        configured = get_configured_kv_store()
        if configured is not None and configured.needs_encryption:
            _store = FernetEncryptionWrapper(
                key_value=configured.store,
                fernet=Fernet(key=derive_shared_fernet_key("workspace-upload-handles")),
            )
            logger.info(
                "Upload handles: using encrypted shared %s store (%s)",
                configured.backend,
                configured.detail,
            )
        else:
            from key_value.aio.stores.memory import MemoryStore

            _store = MemoryStore()
            logger.info(
                "Upload handles: no shared backend configured, using in-process "
                "store (single-instance only)."
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Upload handle store unavailable: %s", type(exc).__name__)
        _store = None
    return _store


async def store_session(
    session_url: str, user_email: str, mime_type: str
) -> Optional[str]:
    """Store a validated session URL under a fresh handle; None on any failure."""
    if (
        not is_valid_session_url(session_url)
        or not user_email
        or not _valid_mime_type(mime_type)
    ):
        return None
    store = _build_store()
    if store is None:
        return None
    now = time.time()
    handle = secrets.token_urlsafe(_HANDLE_BYTES)
    record = {
        "gurl": session_url,
        "sub": user_email,
        "mt": mime_type,
        "exp": now + _FIRST_USE_SECONDS,
        "hard_exp": now + _MAX_LIFETIME_SECONDS,
        "state": "new",
        "bytes": 0,
        "reqs": 0,
    }
    try:
        await store.put(
            handle, record, collection=_COLLECTION, ttl=_MAX_LIFETIME_SECONDS
        )
    except Exception as exc:
        logger.warning("Failed to store upload handle: %s", type(exc).__name__)
        return None
    return handle


async def _load(handle: str) -> Optional[dict]:
    """The record for a live handle, else None. Fails closed on anything odd."""
    if not isinstance(handle, str) or not _HANDLE_RE.fullmatch(handle):
        return None
    store = _build_store()
    if store is None:
        return None
    try:
        record = await store.get(handle, collection=_COLLECTION)
    except Exception as exc:
        logger.warning("Failed to load upload handle: %s", type(exc).__name__)
        return None
    if not isinstance(record, dict):
        return None
    now = time.time()
    exp, hard_exp = record.get("exp"), record.get("hard_exp")
    if not all(isinstance(v, (int, float)) for v in (exp, hard_exp)):
        return None
    if min(exp, hard_exp) < now or hard_exp > now + _MAX_LIFETIME_SECONDS + 60:
        return None
    if not is_valid_session_url(record.get("gurl")):
        return None
    if not (isinstance(record.get("sub"), str) and record["sub"]):
        return None
    if not _valid_mime_type(record.get("mt")):
        return None
    if record.get("state") not in ("new", "active", "done", "dead"):
        return None
    counts = [record.get(k) for k in ("bytes", "reqs")]
    if not all(
        isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts
    ):
        return None
    reply = record.get("reply")
    if reply is not None and not _valid_reply(reply):
        return None
    return record


def _valid_reply(reply: object) -> bool:
    """Google's completion reply, kept so a caller that missed it can ask again."""
    return (
        isinstance(reply, dict)
        and reply.get("status") in (200, 201)
        and isinstance(reply.get("body"), str)
        and len(reply["body"]) <= _MAX_RESPONSE_BYTES
        and isinstance(reply.get("type"), str)
        and len(reply["type"]) <= 255
        and reply["type"].isascii()
        and reply["type"].isprintable()
    )


async def _save(handle: str, record: dict) -> None:
    """Best effort: the upload already happened, so a failed write only loses
    bookkeeping (Google's session stays the arbiter)."""
    store = _build_store()
    ttl = record["hard_exp"] - time.time()
    if store is None or ttl <= 0:
        return
    try:
        await store.put(handle, record, collection=_COLLECTION, ttl=ttl)
    except Exception as exc:
        logger.warning("Failed to update upload handle: %s", type(exc).__name__)


# --- Tool side -----------------------------------------------------------------------

_DOC_FROM = "Return a Google resumable-upload session URL"
_DOC_TO = (
    "Return an upload URL on this server (relayed to a Google resumable-upload session)"
)


def describe(func):
    """Innermost tool decorator: while upload links are active, the
    return_upload_url description says the URL is on this server. Read once at
    import, like the share-restriction note, so the entrypoints set the
    transport before importing tools; otherwise the docstring is untouched."""
    if not active():
        return func
    doc = func.__doc__ or ""
    if _DOC_FROM not in doc:
        logger.warning("%s: no return_upload_url text to update", func.__name__)
        return func
    doc = doc.replace(_DOC_FROM, _DOC_TO)
    # update_drive_file: "... URL to PUT the replacement bytes to directly (...)".
    doc = re.sub(
        r"to(\s+)PUT the replacement bytes to directly \(no Authorization header\)",
        r"for\1the replacement bytes (PUT, no Authorization header)",
        doc,
    )
    func.__doc__ = doc.replace(
        " directly (no Authorization header)", " (no Authorization header)"
    )
    return func


def _unavailable_reason() -> Optional[str]:
    if not signed_downloads.enabled():
        return (
            "upload links on this server need signed download links enabled "
            f"({signed_downloads.FLAG_ENV})"
        )
    if not (os.getenv("WORKSPACE_EXTERNAL_URL") or "").strip():
        return "the server has no public URL configured (WORKSPACE_EXTERNAL_URL)"
    try:
        signed_downloads._signing_key()
    except RuntimeError:
        return "the server has no key material for signed links"
    return None


def _error_text(reason: str) -> str:
    return (
        f"Error: no upload URL could be issued, so nothing will be uploaded: "
        f"{reason}. Do not retry against Google directly; report this to the "
        "server operator."
    )


def _size_text(n: int) -> str:
    mib = n / (1024 * 1024)
    return f"{mib:g} MiB" if mib >= 1 else f"{n} bytes"


async def offer_result(
    summary: str, session_url: str, mime_type: str, user_email: str
) -> str:
    """Tool result for a session opened with the setting on: a ``/ul/`` link,
    or an ``Error:`` line. Never Google's URL."""
    reason = _unavailable_reason()
    if reason:
        return _error_text(reason)
    # The route makes the same check per request; a link it would refuse is no link.
    if not await _owner_usable(user_email):
        return _error_text(
            "this server could not recover usable credentials for you (none "
            "stored here, or an access token about to expire with no refresh "
            "token), and the upload link needs them"
        )
    if not is_valid_session_url(session_url):
        logger.warning("Upload session URL failed validation; not offered.")
        return _error_text("Google returned an unexpected upload session URL")
    handle = await store_session(session_url, user_email, mime_type)
    if handle is None:
        return _error_text("the upload link could not be stored")
    url = f"{signed_downloads._base_url()}/ul/{handle}"
    cap = max_bytes()
    return (
        f"{summary}\n\n"
        f"Upload URL (on this server; no Authorization header or cookies):\n{url}\n\n"
        "Send the bytes with a single PUT, e.g.:\n"
        "  curl --fail-with-body -w '\\nHTTP %{http_code}\\n' "
        f"-H 'Content-Type: {mime_type}' --upload-file \"/path/to/file\" '{url}'\n\n"
        f"The file may be at most {_size_text(cap)}. Content-Length is required "
        "(curl sends it for a regular file; chunked bodies get 411). Success is "
        "HTTP 200 or 201 with the file's metadata as JSON (including its id); the "
        "file is created or updated only then. Start within "
        f"{_FIRST_USE_SECONDS // 60} minutes; the link then stays valid while in "
        f"use, for at most {_MAX_LIFETIME_SECONDS // 3600} hours, and closes once "
        "the upload completes. If a PUT is cut off, send a PUT with "
        "'Content-Range: bytes */<total size>' and 'Content-Length: 0': a 308 "
        "reply's Range header shows the bytes received (none if absent); then send "
        "the rest with Content-Range. HTTP 410 means the link is closed: call this "
        "tool again for a new one."
    )


# --- Route ---------------------------------------------------------------------------


class _ClientBodyError(Exception):
    """The caller's body did not arrive as declared."""


def _error(status: int, message: str) -> Response:
    return JSONResponse(
        {"error": message}, status_code=status, headers=_RESPONSE_HEADERS
    )


def _check_content_range(value: str, length: int, cap: int) -> Optional[Response]:
    match = _CONTENT_RANGE_RE.fullmatch(value.strip())
    if not match:
        return _error(400, "Malformed Content-Range")
    start, end, total = match.groups()
    if total != "*" and int(total) > cap:
        return _error(413, f"File exceeds this server's upload limit of {cap} bytes")
    if start is None:
        # Status query: "bytes */total" carries no body.
        if total == "*" or length != 0:
            return _error(400, "A Content-Range status query has no body")
        return None
    first, last = int(start), int(end)
    if last < first or (total != "*" and last >= int(total)):
        return _error(400, "Content-Range is inconsistent")
    if last >= cap:
        return _error(413, f"File exceeds this server's upload limit of {cap} bytes")
    if length != last - first + 1:
        return _error(400, "Content-Length does not match Content-Range")
    return None


async def _owner_usable(user_email: str) -> bool:
    """Revocation parity with ``/dl/``: the owner's stored credentials still
    exist and are usable. Not a Google-side revocation check (refreshable
    credentials always count as usable)."""
    credentials = await asyncio.to_thread(
        signed_downloads._recover_credentials, user_email
    )
    return credentials is not None and signed_downloads.usable_seconds(credentials) > 0


async def _read_capped(response: httpx.Response) -> tuple[bytes, bool]:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > _MAX_RESPONSE_BYTES:
            return b"", False
    return bytes(data), True


def _client_body(request: Request, length: int, counter: list[int], deadline: float):
    """The caller's body as it arrives, never buffered whole; raises if it ends
    short, runs long, stalls, or is still arriving at the link's hard expiry."""

    async def body():
        stream = request.stream().__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    stream.__anext__(), timeout=_CLIENT_IDLE_SECONDS
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise _ClientBodyError("client stopped sending") from exc
            if time.time() > deadline:
                raise _ClientBodyError("link expired mid-upload")
            if not chunk:
                continue
            counter[0] += len(chunk)
            if counter[0] > length:
                raise _ClientBodyError("body longer than Content-Length")
            yield chunk
        if counter[0] != length:
            raise _ClientBodyError("body shorter than Content-Length")

    return body()


async def serve(request: Request) -> Response:
    """Relay one PUT (or POST, sent on as PUT) to the handle's Google session."""
    if not active():  # mounted only when on; stay inert if that ever changes
        return _error(404, "Not found")
    handle = request.path_params.get("handle", "")
    if not isinstance(handle, str) or not _HANDLE_RE.fullmatch(handle):
        return _error(403, "Invalid or expired upload link")
    if handle in _in_flight:
        return _error(409, "Another upload to this link is in progress")
    # No await between the check above and this add: one relay per handle in this
    # process, and the record is read only once the handle is held.
    _in_flight.add(handle)
    try:
        return await _serve_held(handle, request)
    finally:
        _in_flight.discard(handle)


def _is_status_query(request: Request) -> bool:
    content_range = (request.headers.get("content-range") or "").strip()
    return (
        request.headers.get("content-length") == "0"
        and _CONTENT_RANGE_RE.fullmatch(content_range) is not None
        and content_range.startswith("bytes */")
        and not content_range.endswith("/*")
    )


async def _serve_held(handle: str, request: Request) -> Response:
    record = await _load(handle)
    if record is None:
        return _error(403, "Invalid or expired upload link")
    if record["state"] == "done":
        reply = record.get("reply")
        if reply and _is_status_query(request):
            # A caller that missed the completion reply can still learn the file id.
            return Response(
                content=reply["body"].encode("utf-8"),
                status_code=reply["status"],
                headers={**_RESPONSE_HEADERS, "Content-Type": reply["type"]}
                if reply["type"]
                else _RESPONSE_HEADERS,
            )
        return _error(410, "This upload link has been used; the upload completed.")
    if record["state"] == "dead" or record["reqs"] >= _MAX_REQUESTS:
        return _error(
            410,
            "This upload link is closed. Call the tool again for a new upload URL.",
        )

    if "transfer-encoding" in request.headers:
        return _error(411, "Chunked bodies are not accepted; send Content-Length")
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        return _error(411, "Content-Length is required")
    if not (raw_length.isascii() and raw_length.isdigit()) or len(raw_length) > 15:
        return _error(400, "Malformed Content-Length")
    length = int(raw_length)
    cap = max_bytes()
    if length > cap:
        return _error(413, f"File exceeds this server's upload limit of {cap} bytes")
    content_range = request.headers.get("content-range")
    if content_range is None and length == 0:
        # Google would take a bare empty PUT as the whole file and complete it.
        return _error(
            400,
            "An empty request needs Content-Range: 'bytes */<total size>' asks "
            "how much has arrived. For an empty file, use the tool's inline "
            "content instead.",
        )
    if content_range is not None:
        problem = _check_content_range(content_range, length, cap)
        if problem is not None:
            return problem
    if record["bytes"] + length > 2 * cap:
        return _error(413, "This link has carried its maximum total upload volume")

    if not await _owner_usable(record["sub"]):
        return _error(
            401, "The upload owner's credentials are not recoverable on this server"
        )
    return await _relay(handle, record, request, length, content_range)


async def _relay(
    handle: str,
    record: dict,
    request: Request,
    length: int,
    content_range: Optional[str],
) -> Response:
    headers = {"Content-Length": str(length), "Content-Type": record["mt"]}
    if content_range is not None:
        headers["Content-Range"] = content_range.strip()
    sent = [0]
    content = _client_body(request, length, sent, record["hard_exp"]) if length else b""
    client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT, follow_redirects=False)
    status: Optional[int] = None
    body, complete, google_type, google_range = b"", False, "", ""
    try:
        upstream = await client.send(
            client.build_request(
                "PUT", record["gurl"], headers=headers, content=content
            ),
            stream=True,
        )
        try:
            # Google has answered: its status decides the outcome even if the
            # (small) reply body then fails to arrive.
            status = upstream.status_code
            google_type = upstream.headers.get("content-type", "")
            google_range = upstream.headers.get("range", "")
            try:
                body, complete = await _read_capped(upstream)
            except httpx.HTTPError:
                body, complete = b"", False
        finally:
            await upstream.aclose()
    except (_ClientBodyError, ClientDisconnect) as exc:
        logger.info("Upload relay: caller's body incomplete (%s)", type(exc).__name__)
        response = _error(400, "The request body did not arrive as declared")
    except httpx.TimeoutException:
        logger.warning("Upload relay: Google timed out")
        response = _error(504, "Timed out waiting for Google")
    except httpx.HTTPError as exc:
        logger.warning("Upload relay: Google request failed (%s)", type(exc).__name__)
        response = _error(502, "The upload to Google failed")
    except Exception as exc:  # never a traceback (it could carry the session URL)
        logger.warning("Upload relay: unexpected failure (%s)", type(exc).__name__)
        response = _error(502, "The upload to Google failed")
    else:
        response = _upstream_response(status, body, complete, google_type, google_range)
    finally:
        await client.aclose()

    now = time.time()
    record["reqs"] += 1
    record["bytes"] += sent[0]
    if record["state"] == "new":
        record["state"] = "active"
    if status in (200, 201):
        record["state"] = "done"
        if complete:
            record["reply"] = {
                "status": status,
                "body": body.decode("utf-8", errors="replace"),
                "type": google_type if _valid_mime_type(google_type) else "",
            }
    elif status is not None and _closes_link(status):
        record["state"] = "dead"
    record["exp"] = min(now + _IDLE_SECONDS, record["hard_exp"])
    await _save(handle, record)
    logger.info(
        "Upload relay for %s: %d bytes, Google status %s, link %s",
        record["sub"],
        sent[0],
        status if status is not None else "none",
        record["state"],
    )
    return response


def _closes_link(status: int) -> bool:
    """A 4xx means Google will not take more on this session (408 and 429 ask
    for a retry instead)."""
    return 400 <= status < 500 and status not in (408, 429)


def _upstream_response(
    status: int,
    body: bytes,
    complete: bool,
    google_type: str,
    google_range: str,
) -> Response:
    if status in (200, 201):
        if not complete:
            return JSONResponse(
                {"status": "uploaded", "note": "Google's reply could not be relayed"},
                status_code=status,
                headers=_RESPONSE_HEADERS,
            )
        headers = dict(_RESPONSE_HEADERS)
        if google_type and "\n" not in google_type and "\r" not in google_type:
            headers["Content-Type"] = google_type
        return Response(content=body, status_code=status, headers=headers)
    if status == 308:
        headers = dict(_RESPONSE_HEADERS)
        if _RANGE_RE.fullmatch(google_range):
            headers["Range"] = google_range
        return Response(status_code=308, headers=headers)
    if status in (408, 429):
        return _error(503, "Google asked to retry later; query progress, then resume")
    if _closes_link(status):
        return _error(
            410,
            f"Google refused the upload (HTTP {status}); this link is now closed. "
            "Call the tool again for a new upload URL.",
        )
    return _error(502, f"Unexpected reply from Google (HTTP {status})")
