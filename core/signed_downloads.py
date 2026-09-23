"""Signed, short-lived download URLs that stream Google content on demand.

Neither Gmail nor Drive offers a public download URL: bytes only come back from
an authenticated API call. Without this module a remote server must either write
the file to local disk and serve it from ``/attachments/{id}`` (impossible in
stateless mode, and not tied to a user) or hand base64 back through the model.

With ``WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS=true`` the download tools instead
return ``/attachments/signed/{token}``: a Fernet token (authenticated
encryption) naming the resource, its owner (``sub``) and an expiry. Without
the key the link reveals only when it was minted, not its owner or file. The route decrypts and authenticates the
token, recovers the owner's credentials (the in-process session store first,
then the persistent credential store), fetches from Google and returns the
bytes. The token is the authorization; the route never writes to either store.

Mint and serve share ONE notion of "usable credentials" (``usable_seconds``): a
URL is only minted while the route would accept it, and its TTL is clamped so it
dies before that acceptance ends. Credentials that carry a refresh token are
refreshed in memory by the route, so their URLs get the full lifetime.
"""

import asyncio
import base64
import binascii
import functools
import io
import json
import logging
import math
import os
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import quote, urlparse

from cryptography.fernet import Fernet
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from core.attachment_storage import external_base_url
from core.config import get_transport_mode
from core.file_limits import FileTooLargeError, ensure_within_file_size_limit

try:
    from google.auth._helpers import REFRESH_THRESHOLD
except ImportError:  # private google-auth API: keep this module importable
    # google-auth 2.x's value; TestUsability pins it against the installed release.
    REFRESH_THRESHOLD = timedelta(seconds=225)

logger = logging.getLogger(__name__)

FLAG_ENV = "WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS"
_KEY_SALT = "workspace-mcp-signed-download"
URL_TTL_SECONDS = 900
# A URL must expire at least this long before the credential stops being usable.
_EXPIRY_MARGIN_SECONDS = 30
# A link that would live less than this is not worth handing to an agent that has
# yet to fetch it; the tool falls back to its stored copy instead.
_MIN_TTL_SECONDS = 60
# Bound on names carried in a token, so a long sender-controlled filename cannot
# push the token past _MAX_TOKEN_CHARS.
_MAX_FILENAME_CHARS = 200
# Concurrent downloads the public route will run; beyond this it answers 503.
_MAX_CONCURRENT_DOWNLOADS = 4
# Owners whose stored access token the route refreshed in memory, so repeat
# fetches of a link reuse the refreshed copy instead of refreshing again.
_REFRESHED_CACHE_SIZE = 256
# Tolerated clock difference between the replica that minted a token and the one
# serving it: ``iat`` may sit this far in the future, and Fernet's own timestamp
# may be this much older than ``URL_TTL_SECONDS`` before the token is refused.
_CLOCK_SKEW_SECONDS = 30
# Hard ceiling on a token's age, checked by Fernet from its own timestamp before
# the payload is decrypted, so a token never outlives the maximum link lifetime
# even if its ``exp`` claim were wrong.
_MAX_TOKEN_AGE_SECONDS = URL_TTL_SECONDS + _CLOCK_SKEW_SECONDS
# Bound on what the route will even try to decrypt; minting refuses to exceed it.
_MAX_TOKEN_CHARS = 8192
_RESERVED_CLAIMS = frozenset({"src", "sub", "iat", "exp", "fn", "mt"})
# Sender-typed bytes on a public route: never sniff, never cache (success or error).
_RESPONSE_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}


def _flag_set() -> bool:
    # Stray whitespace from YAML or .env files must not silently disable the flag.
    return os.getenv(FLAG_ENV, "").strip().lower() == "true"


def enabled() -> bool:
    """Opt-in, and only over streamable-http: the stdio callback server does not
    mount this route, and a local server can hand out file paths instead."""
    return _flag_set() and get_transport_mode() == "streamable-http"


def log_if_ignored(
    transport: str, notice: Optional[Callable[[str], None]] = None
) -> None:
    """Run once from ``main()``: a stdio operator who sets the flag gets one line
    saying it is ignored, instead of URLs that are silently never offered.
    ``notice`` routes the line to the startup screen instead of the log."""
    if _flag_set() and transport != "streamable-http":
        message = (
            f"{FLAG_ENV} is set but the transport is {transport}; signed download "
            "URLs are only issued over streamable-http, so the setting is ignored."
        )
        (notice or logger.warning)(message)


def validate_startup(
    transport: str, notice: Optional[Callable[[str], None]] = None
) -> None:
    """Run once from each server entrypoint: with the flag on over streamable-http,
    raise ``ValueError`` (one line per problem) unless every minted link can work,
    then report the base URL links will use (to ``notice`` when given, else the
    log). Flag off or stdio: no check, no report.

    Links are absolute URLs clients open outside the MCP session, so they need an
    externally reachable base; the ``host:port`` fallback is the bind address, not
    something a client can reach. The key check calls ``_signing_key`` itself, so
    it cannot disagree with minting. Service-account mode keeps no per-user
    credentials for the route to recover, so no link could ever be served."""
    if not _flag_set() or transport != "streamable-http":
        return
    from auth.oauth_config import is_service_account_enabled

    problems = []
    if is_service_account_enabled():
        problems.append(
            f"{FLAG_ENV}=true needs per-user OAuth credentials and cannot work in "
            "service-account (domain-wide delegation) mode; unset one of them."
        )
    external = os.getenv("WORKSPACE_EXTERNAL_URL")
    if not external or not external.strip():
        problems.append(
            f"{FLAG_ENV}=true requires WORKSPACE_EXTERNAL_URL; set it to the "
            "absolute http:// or https:// URL clients reach this server at "
            "(e.g. https://mcp.example.com)."
        )
    else:
        parsed = urlparse(external)
        # urlparse strips surrounding whitespace, but the links would keep it.
        if (
            external != external.strip()
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
        ):
            problems.append(
                f"Invalid WORKSPACE_EXTERNAL_URL={external!r} for {FLAG_ENV}=true; "
                "expected an absolute http:// or https:// URL "
                "(e.g. https://mcp.example.com)."
            )
    try:
        _signing_key()
    except RuntimeError:
        problems.append(
            f"{FLAG_ENV}=true requires signing key material; set "
            "GOOGLE_OAUTH_CLIENT_SECRET or FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY."
        )
    if problems:
        raise ValueError("\n".join(problems))
    (notice or logger.info)(
        f"{FLAG_ENV} is on: signed download links will use base URL "
        f"{external_base_url()}; /attachments/signed/* must be publicly reachable there."
    )


@functools.lru_cache(maxsize=1)
def _signing_key() -> bytes:
    """Derive the Fernet key from the OAuth proxy's key material under a dedicated
    salt, so it is isolated from the server's other derived keys.

    ``derive_jwt_key`` returns the URL-safe base64 of exactly 32 derived bytes
    (HKDF-SHA256 for high-entropy material, PBKDF2 for low-entropy), which is
    precisely Fernet's key encoding, so the result is used as the key as-is.
    """
    from auth.oauth_config import get_oauth_config
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key

    override = os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", "").strip()
    if override:
        return derive_jwt_key(low_entropy_material=override, salt=_KEY_SALT)
    secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip() or (
        get_oauth_config().client_secret
    )
    if secret:
        return derive_jwt_key(high_entropy_material=secret, salt=_KEY_SALT)
    raise RuntimeError(
        "Signed download URLs need GOOGLE_OAUTH_CLIENT_SECRET or "
        "FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY to derive a signing key."
    )


# --- Credential usability: the ONE predicate mint and serve share -------------------


def _refreshable(credentials: Credentials) -> bool:
    """google-auth can only refresh with all four of these present."""
    return bool(
        credentials.refresh_token
        and credentials.token_uri
        and credentials.client_id
        and credentials.client_secret
    )


def usable_seconds(
    credentials: Credentials, *, now: Optional[datetime] = None
) -> float:
    """Seconds the route can still use these credentials; <= 0 means it cannot.

    Refreshable credentials are usable indefinitely: the route refreshes them in
    memory before fetching. Otherwise they are usable only while google-auth still
    treats the access token as valid, which ends ``REFRESH_THRESHOLD`` (3m45s)
    BEFORE ``expiry`` — past that point the API client would try to refresh and
    fail. An access token with no recorded expiry and no way to refresh is treated
    as unusable: Google expires it within the hour, and nothing here can tell when.
    ``expiry`` is google-auth's naive-UTC ``Credentials.expiry``.
    """
    if _refreshable(credentials):
        return math.inf
    if not credentials.token or credentials.expiry is None:
        return 0.0
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    expiry = credentials.expiry.replace(tzinfo=timezone.utc)
    return (expiry - REFRESH_THRESHOLD - ref).total_seconds()


def _max_link_seconds() -> int:
    """Longest a link may live: ``URL_TTL_SECONDS``, shortened to the MCP access
    token lifetime when an operator configured a shorter one, so a link cannot
    keep serving long after the session that minted it would have lapsed."""
    from auth.oauth_proxy_config import (
        MAX_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS,
        OAUTH_ACCESS_TOKEN_EXPIRY_ENV,
        _parse_expiry_seconds_env,
    )

    session_seconds = _parse_expiry_seconds_env(
        OAUTH_ACCESS_TOKEN_EXPIRY_ENV,
        minimum=1,
        maximum=MAX_OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS,
    )
    return min(URL_TTL_SECONDS, session_seconds or URL_TTL_SECONDS)


def clamp_ttl(credentials: Credentials, *, now: Optional[datetime] = None) -> int:
    """URL lifetime that ends before the credentials stop being usable, or 0 (do
    not mint) when that leaves less than ``_MIN_TTL_SECONDS``. Never raised to the
    minimum: a floor would let a URL outlive the credentials that serve it."""
    left = usable_seconds(credentials, now=now) - _EXPIRY_MARGIN_SECONDS
    ttl = int(min(_max_link_seconds(), left))
    return ttl if ttl >= _MIN_TTL_SECONDS else 0


def format_ttl(seconds: float) -> str:
    return (
        f"{int(seconds)} seconds" if seconds < 120 else f"~{int(seconds // 60)} minutes"
    )


def _bounded_filename(name: Optional[str]) -> Optional[str]:
    """Shorten a name to ``_MAX_FILENAME_CHARS``, keeping its extension."""
    if not name or len(name) <= _MAX_FILENAME_CHARS:
        return name
    suffix = PurePath(name).suffix
    if len(suffix) > 16:
        suffix = ""
    return name[: _MAX_FILENAME_CHARS - len(suffix)] + suffix


def mint_url(
    *,
    source: str,
    user_email: str,
    ref: dict,
    ttl_seconds: int,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> str:
    """Encrypt a capability URL for one resource (``ref``: fetcher-specific
    locator) and owner. The claims travel as compact JSON inside the token; the
    Fernet timestamp is set to ``iat`` so both clocks agree."""
    collisions = _RESERVED_CLAIMS & ref.keys()
    if collisions:
        raise ValueError(f"ref must not contain reserved claims: {sorted(collisions)}")
    now = int(time.time())
    claims = {
        "src": source,
        "sub": user_email,
        "iat": now,
        "exp": now + ttl_seconds,
        **ref,
    }
    filename = _bounded_filename(filename)
    if filename:
        claims["fn"] = filename
    if mime_type:
        claims["mt"] = mime_type
    payload = json.dumps(claims, separators=(",", ":"), ensure_ascii=False)
    # Padding is dropped from the URL: a trailing '=' is easily lost when a link
    # is copied or auto-linked, and verify_token restores it.
    token = (
        Fernet(_signing_key())
        .encrypt_at_time(payload.encode("utf-8"), now)
        .decode("ascii")
        .rstrip("=")
    )
    if len(token) > _MAX_TOKEN_CHARS:
        raise ValueError("download token too large for the route to accept")
    return f"{external_base_url()}/attachments/signed/{token}"


def _is_timestamp(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def verify_token(token: str) -> Optional[dict]:
    """Claims for a valid token, else None (tampered, wrong key, expired,
    malformed, no key). Every check happens before any credential lookup.

    The token must be the canonical URL-safe base64 Fernet emitted, with or
    without its padding (the decoder would otherwise ignore bytes appended after
    the padding). Fernet then
    authenticates it and refuses one older than ``_MAX_TOKEN_AGE_SECONDS`` by its
    own timestamp (the hard ceiling); then the decrypted claims must carry a
    non-empty ``sub`` and integer ``iat``/``exp``, ``exp`` must still be in the
    future and ``iat`` may not lead the clock by more than the skew allowance.
    """
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_CHARS:
        return None
    now = int(time.time())
    try:
        raw = token.encode("ascii").rstrip(b"=")
        raw += b"=" * (-len(raw) % 4)
        if base64.urlsafe_b64encode(base64.urlsafe_b64decode(raw)) != raw:
            return None
        payload = Fernet(_signing_key()).decrypt_at_time(
            raw, ttl=_MAX_TOKEN_AGE_SECONDS, current_time=now
        )
        claims = json.loads(payload.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None
    sub, iat, exp = claims.get("sub"), claims.get("iat"), claims.get("exp")
    if not (isinstance(sub, str) and sub):
        return None
    if not (_is_timestamp(iat) and _is_timestamp(exp)):
        return None
    if exp <= now or iat > now + _CLOCK_SKEW_SECONDS:
        return None
    return claims


def _recover_credentials(user_email: str) -> Optional[Credentials]:
    """The owner's credentials: session store first, then the persistent
    credential store (skipped in stateless mode, where nothing is persisted).

    Read-only. Both stores hand back a fresh ``Credentials`` object, so refreshing
    it later never reaches storage. The tool and the route both use this lookup,
    so a URL is only offered where the route can recover the owner.
    """
    from auth.credential_store import get_credential_store
    from auth.oauth21_session_store import get_oauth21_session_store
    from auth.oauth_config import is_stateless_mode

    try:
        credentials = get_oauth21_session_store().get_credentials(user_email)
        if credentials is None and not is_stateless_mode():
            credentials = get_credential_store().get_credential(user_email)
        return credentials
    except Exception as exc:
        logger.debug("Could not recover credentials for %s: %s", user_email, exc)
        return None


@dataclass(frozen=True)
class Offer:
    """What ``offer_url`` could issue: a URL and its lifetime, or the reason
    there is none (empty when the feature is simply off). Falsy without a URL."""

    url: Optional[str] = None
    ttl: int = 0
    reason: str = ""

    def __bool__(self) -> bool:
        return self.url is not None


NO_CREDENTIALS = (
    "this server holds no stored Google credentials for your account; "
    "re-authenticate through this server if this persists"
)
SHORT_LIVED_CREDENTIALS = (
    "your stored access token expires too soon and has no refresh token to renew "
    "it; re-authenticate through this server to get links again"
)


async def offer_url(
    user_email: str,
    *,
    source: str,
    ref: dict,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Offer:
    """Tool-side entry point: an ``Offer`` with a URL, or one saying why not so the
    tool can use its normal path and tell the user.

    No URL when the feature is off, or when the route could not serve one: the
    owner's credentials are recoverable from neither store, they would stay usable
    for less than ``_MIN_TTL_SECONDS``, or the token cannot be minted.
    """
    if not enabled():
        return Offer()
    credentials = await asyncio.to_thread(_recover_credentials, user_email)
    if credentials is None:
        reason = NO_CREDENTIALS
    elif clamp_ttl(credentials) <= 0:
        reason = SHORT_LIVED_CREDENTIALS
    else:
        ttl = clamp_ttl(credentials)
        try:
            return Offer(
                mint_url(
                    source=source,
                    user_email=user_email,
                    ref=ref,
                    ttl_seconds=ttl,
                    filename=filename,
                    mime_type=mime_type,
                ),
                ttl,
            )
        except (RuntimeError, ValueError) as exc:
            logger.warning("Signed download URL unavailable: %s", exc)
            return Offer(reason=f"the server could not create the link ({exc})")
    logger.info("Signed download URL unavailable: %s", reason)
    return Offer(reason=reason)


def url_lines(url: str, ttl: int, what: str) -> list[str]:
    """Result lines shared by the tools that hand out a signed URL."""
    return [
        f"\n📎 Download URL: {url}",
        f"\nThe server fetches the {what} directly from Google when this URL is "
        f"requested; the link is tied to your account and expires in {format_ttl(ttl)}. "
        "Fetch it promptly; do not queue it for later.",
    ]


def unavailable_note(offer: Offer) -> str:
    """The line a tool adds when it fell back from a signed URL, naming why."""
    return f"\n⚠️ No signed download URL could be issued: {offer.reason}."


# --- Fetchers: claims + owner credentials -> bytes -------------------------------


@dataclass
class DownloadResult:
    """A buffered body or a bounded-memory stream, never both. ``close`` releases
    the API client once the body has been sent."""

    filename: str
    media_type: str
    content: Optional[bytes] = None
    stream: Optional[AsyncIterator[bytes]] = None
    close: Optional[Callable[[], None]] = None


class SignedDownloadError(Exception):
    """A fetcher could not produce the bytes. ``status`` and ``public`` are what the
    route returns: a caller-safe reason for failures the caller can act on (the
    resource is gone, not downloadable, empty), else a generic 502."""

    def __init__(
        self, message: str, *, status: int = 502, public: Optional[str] = None
    ):
        super().__init__(message)
        self.status = status
        self.public = public or "Failed to fetch the requested resource"


# Google API error reasons the caller can act on, and what to tell them.
_ACTIONABLE_REASONS = {
    "fileNotDownloadable": "This Drive item cannot be downloaded in its stored form.",
    "exportSizeLimitExceeded": "This file is too large for Google to export.",
    "cannotDownloadFile": "The owner has disabled downloading this file.",
}


def _google_error(exc: Exception, what: str) -> SignedDownloadError:
    """Map a Google API failure to a route error, naming it when actionable."""
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        reason = getattr(exc, "reason", "") or ""
        for code, public in _ACTIONABLE_REASONS.items():
            if code in str(exc) or code == reason:
                return SignedDownloadError(
                    f"{what} failed: {exc}", status=422, public=public
                )
        if status == 404:
            return SignedDownloadError(
                f"{what} failed: {exc}",
                status=404,
                public=f"The {what.split()[0]} item no longer exists or is not accessible.",
            )
    return SignedDownloadError(f"{what} failed: {exc}")


def _tool_module(name: str):
    """A tool module this server already loaded, or None.

    Importing a tool module registers its tools on the server as a side effect, so
    the public route must never be the first to import one: a server started
    without Gmail must not grow Gmail tools because a Gmail token arrived. A source
    whose tools this server does not load is simply not served here."""
    return sys.modules.get(name)


def _close_service(service) -> None:
    from auth.service_decorator import _release_google_service_cycles

    try:
        service.close()
    finally:
        _release_google_service_cycles()


def _within_file_limit(content: bytes, what: str) -> bytes:
    """Buffered fetches enforce ``WORKSPACE_MCP_MAX_FILE_BYTES`` on the actual bytes:
    the size checked at mint time is Google's declaration, which can be absent or
    understated (``sizeEstimate`` is approximate)."""
    try:
        ensure_within_file_size_limit(len(content), kind=what)
    except FileTooLargeError as exc:
        raise SignedDownloadError(
            f"{what} exceeds the file size limit",
            status=413,
            public=f"The {what} exceeds this server's file size limit.",
        ) from exc
    return content


def _decode_urlsafe(data: str, what: str) -> bytes:
    """Gmail's URL-safe base64, possibly unpadded; an empty part decodes to b""."""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SignedDownloadError(f"{what} decode failed: {exc}") from exc


async def _fetch_gmail_attachment(
    claims: dict, credentials: Credentials
) -> DownloadResult:
    """Gmail returns the attachment as one JSON response: buffered, not streamed."""
    message_id, attachment_id = claims.get("mid"), claims.get("aid")
    if not (message_id and attachment_id):
        raise SignedDownloadError("Gmail token missing mid/aid")
    gmail = await asyncio.to_thread(build, "gmail", "v1", credentials=credentials)
    try:
        try:
            attachment = await asyncio.to_thread(
                gmail.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute
            )
        except Exception as exc:
            raise _google_error(exc, "Gmail attachment fetch") from exc
        content = _within_file_limit(
            _decode_urlsafe(attachment.get("data", ""), "Gmail attachment"),
            "attachment",
        )
    finally:
        _close_service(gmail)
    return DownloadResult(
        filename=claims.get("fn") or "attachment",
        media_type=claims.get("mt") or "application/octet-stream",
        content=content,
    )


async def _fetch_gmail_message(
    claims: dict, credentials: Credentials
) -> DownloadResult:
    """A complete message in the export representation the tool offered (``fmt``).

    The extension follows what was actually rendered — an ``html`` export of a
    message with no HTML part falls back to plain text — so the token's ``fn`` is
    the name without one."""
    gmail_tools = _tool_module("gmail.gmail_tools")
    if gmail_tools is None:
        raise SignedDownloadError(
            "Gmail tools are not loaded on this server",
            status=404,
            public="Not found",
        )
    message_id, body_format = claims.get("mid"), claims.get("fmt")
    if not message_id or body_format not in ("raw", "html", "text"):
        raise SignedDownloadError("Gmail message token missing mid/fmt")
    gmail = await asyncio.to_thread(build, "gmail", "v1", credentials=credentials)
    try:
        try:
            (
                content,
                mime_type,
                extension,
                _notes,
            ) = await gmail_tools._render_message_export(gmail, message_id, body_format)
        except ValueError as exc:
            # _render_message_export's ValueErrors are its user-facing reasons.
            raise SignedDownloadError(
                f"Gmail message export failed: {exc}",
                status=422,
                public=f"The message {exc}",
            ) from exc
        except Exception as exc:
            raise _google_error(exc, "Gmail message export") from exc
        content = _within_file_limit(content, "message export")
    finally:
        _close_service(gmail)
    return DownloadResult(
        filename=f"{claims.get('fn') or 'message'}{extension}",
        media_type=mime_type,
        content=content,
    )


async def _fetch_drive(claims: dict, credentials: Credentials) -> DownloadResult:
    """Stream a Drive file in bounded chunks; ``emt`` set means export a native file."""
    drive_tools = _tool_module("gdrive.drive_tools")
    if drive_tools is None:
        raise SignedDownloadError(
            "Drive tools are not loaded on this server", status=404, public="Not found"
        )
    file_id = claims.get("fid")
    if not file_id:
        raise SignedDownloadError("Drive token missing fid")
    export_mime = claims.get("emt")
    drive = await asyncio.to_thread(build, "drive", "v3", credentials=credentials)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buffer,
        drive_tools._media_request(drive, file_id, export_mime),
        chunksize=drive_tools.DOWNLOAD_CHUNK_SIZE,
    )

    def next_chunk() -> tuple[bytes, bool]:
        # next_chunk() appends to the buffer; drain it so memory stays at one chunk.
        _status, done = downloader.next_chunk()
        chunk = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        return chunk, done

    # Pull the first chunk eagerly so auth / not-found errors become an error
    # status instead of a truncated 200.
    try:
        chunk, done = await asyncio.to_thread(next_chunk)
    except Exception as exc:
        _close_service(drive)
        raise _google_error(exc, "Drive download") from exc

    async def body() -> AsyncIterator[bytes]:
        pending, finished = chunk, done
        while True:
            if pending:
                yield pending
            if finished:
                return
            try:
                pending, finished = await asyncio.to_thread(next_chunk)
            except Exception as exc:
                logger.error("Drive stream interrupted mid-download: %s", exc)
                # Headers are out. Raising makes the server abort the chunked body
                # without its terminating chunk, so the client sees an incomplete
                # transfer; returning would end it cleanly as a complete file.
                raise SignedDownloadError(f"Drive stream interrupted: {exc}") from exc

    return DownloadResult(
        filename=claims.get("fn") or "download",
        media_type=claims.get("mt") or export_mime or "application/octet-stream",
        stream=body(),
        close=lambda: _close_service(drive),
    )


_FETCHERS: dict[str, Callable[[dict, Credentials], Awaitable[DownloadResult]]] = {
    "gmail": _fetch_gmail_attachment,
    "gmail_message": _fetch_gmail_message,
    "drive": _fetch_drive,
}


# --- Route --------------------------------------------------------------------------


def _content_disposition(name: str) -> str:
    """Names come from Gmail/Drive metadata (sender-controlled): the ASCII
    ``filename=`` keeps printable ASCII only — no quotes, backslashes, C0/DEL
    controls (h11 refuses a header value containing NUL) or non-ASCII, which the
    percent-encoded ``filename*`` carries intact."""
    ascii_name = (
        "".join(ch for ch in name if 0x20 <= ord(ch) < 0x7F and ch not in '"\\')
        or "download"
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def _error(status: int, message: str, headers: Optional[dict] = None) -> Response:
    return JSONResponse(
        {"error": message},
        status_code=status,
        headers={**_RESPONSE_HEADERS, **(headers or {})},
    )


_download_slots: Optional[asyncio.Semaphore] = None
# Refreshed in-memory credentials, keyed by owner. Never written to any store.
_refreshed: "OrderedDict[str, Credentials]" = OrderedDict()


def _slots() -> asyncio.Semaphore:
    global _download_slots
    if _download_slots is None:
        _download_slots = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)
    return _download_slots


async def _usable_credentials(user_email: str) -> Optional[Credentials]:
    """The owner's credentials ready to call Google with, or None.

    A stored access token that has lapsed is refreshed in memory only, and the
    refreshed copy is reused for later fetches while it stays valid and its
    refresh token still matches the stored one (a re-consent replaces it)."""
    credentials = await asyncio.to_thread(_recover_credentials, user_email)
    if credentials is None or usable_seconds(credentials) <= 0:
        return None
    if credentials.valid:
        return credentials
    cached = _refreshed.get(user_email)
    if (
        cached is not None
        and cached.valid
        and cached.refresh_token == credentials.refresh_token
    ):
        _refreshed.move_to_end(user_email)
        return cached
    # usable_seconds() > 0 with an invalid token means refreshable.
    try:
        await asyncio.to_thread(credentials.refresh, Request())
    except Exception as exc:
        logger.warning("Signed download: credential refresh failed: %s", exc)
        raise
    _refreshed[user_email] = credentials
    _refreshed.move_to_end(user_email)
    while len(_refreshed) > _REFRESHED_CACHE_SIZE:
        _refreshed.popitem(last=False)
    return credentials


async def serve(token: str) -> Response:
    """Verify a signed token, then return the resource with its owner's credentials.

    At most ``_MAX_CONCURRENT_DOWNLOADS`` run at once (503 beyond that); a streamed
    body keeps its slot and its API client until the last byte is sent."""
    if not enabled():  # keep the public route inert unless the feature is on
        return _error(404, "Not found")
    claims = verify_token(token)
    fetcher = _FETCHERS.get(claims.get("src", "")) if claims else None
    if not (claims and fetcher and claims.get("sub")):
        return _error(403, "Invalid or expired download link")

    slots = _slots()
    if slots.locked():
        return _error(
            503, "Too many downloads in progress; retry shortly", {"Retry-After": "5"}
        )
    await slots.acquire()
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            slots.release()

    streaming = False
    try:
        # Only a validly signed token naming this user reaches the lookup.
        try:
            credentials = await _usable_credentials(claims["sub"])
        except Exception:
            return _error(
                401, "The download owner's credentials could not be refreshed"
            )
        if credentials is None:
            return _error(
                401,
                "The download owner's credentials are not recoverable on this server",
            )

        try:
            result = await fetcher(claims, credentials)
        except SignedDownloadError as exc:
            logger.error("Signed download fetch failed: %s", exc)
            return _error(exc.status, exc.public)

        headers = {
            **_RESPONSE_HEADERS,
            "Content-Disposition": _content_disposition(result.filename),
        }
        if result.stream is None:
            return Response(
                content=result.content, media_type=result.media_type, headers=headers
            )

        def finish() -> None:
            if released:  # the stream and the background task both call this
                return
            try:
                if result.close:
                    result.close()
            finally:
                release()

        async def body() -> AsyncIterator[bytes]:
            try:
                async for part in result.stream:
                    yield part
            finally:
                finish()

        streaming = True
        # The background task covers a response that ends before its body starts.
        return StreamingResponse(
            body(),
            media_type=result.media_type,
            headers=headers,
            background=BackgroundTask(finish),
        )
    finally:
        if not streaming:
            release()
