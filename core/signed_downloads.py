"""Signed, short-lived download URLs that stream Google content on demand.

Neither Gmail nor Drive offers a public download URL: bytes only come back from
an authenticated API call. Without this module a remote server must either write
the file to local disk and serve it from ``/attachments/{id}`` (impossible in
stateless mode, and not tied to a user) or hand base64 back through the model.

With ``WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS=true`` the download tools instead
return ``/attachments/signed/{token}``: a Fernet token (authenticated
encryption) naming the resource, its owner (``sub``) and an expiry. Nothing in
the link is readable without the key. The route decrypts and authenticates the
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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import quote, urlparse

from cryptography.fernet import Fernet
from fastapi.responses import JSONResponse, Response, StreamingResponse
from google.auth._helpers import REFRESH_THRESHOLD
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from core.config import get_transport_mode

logger = logging.getLogger(__name__)

FLAG_ENV = "WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS"
_KEY_SALT = "workspace-mcp-signed-download"
URL_TTL_SECONDS = 900
# A URL must expire at least this long before the credential stops being usable.
_EXPIRY_MARGIN_SECONDS = 30
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
    return os.getenv(FLAG_ENV, "false").lower() == "true"


def enabled() -> bool:
    """Opt-in, and only over streamable-http: the stdio callback server does not
    mount this route, and a local server can hand out file paths instead."""
    return _flag_set() and get_transport_mode() == "streamable-http"


def log_if_ignored(transport: str) -> None:
    """Run once from ``main()``: a stdio operator who sets the flag gets one line
    saying it is ignored, instead of URLs that are silently never offered."""
    if _flag_set() and transport != "streamable-http":
        logger.warning(
            "%s is set but the transport is %s; signed download URLs are only "
            "issued over streamable-http, so the setting is ignored.",
            FLAG_ENV,
            transport,
        )


def validate_startup(transport: str) -> None:
    """Run once from each server entrypoint: with the flag on over streamable-http,
    raise ``ValueError`` (one line per problem) unless every minted link can work,
    then log the base URL links will use. Flag off or stdio: no check, no log.

    Links are absolute URLs clients open outside the MCP session, so they need an
    externally reachable base; the ``host:port`` fallback is the bind address, not
    something a client can reach. The key check calls ``_signing_key`` itself, so
    it cannot disagree with minting."""
    if not _flag_set() or transport != "streamable-http":
        return
    problems = []
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
    logger.info(
        "%s is on: signed download links will use base URL %s; "
        "/attachments/signed/* must be publicly reachable there.",
        FLAG_ENV,
        _base_url(),
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
    fail. ``expiry`` is google-auth's naive-UTC ``Credentials.expiry``.
    """
    if _refreshable(credentials):
        return math.inf
    if not credentials.token:
        return 0.0
    if credentials.expiry is None:
        return math.inf
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    expiry = credentials.expiry.replace(tzinfo=timezone.utc)
    return (expiry - REFRESH_THRESHOLD - ref).total_seconds()


def clamp_ttl(credentials: Credentials, *, now: Optional[datetime] = None) -> int:
    """URL lifetime that ends before the credentials stop being usable; <= 0 means
    do not mint. Deliberately not floored: a minimum would let a URL outlive the
    credentials that serve it."""
    left = usable_seconds(credentials, now=now) - _EXPIRY_MARGIN_SECONDS
    return int(min(URL_TTL_SECONDS, left))


def format_ttl(seconds: float) -> str:
    return (
        f"{int(seconds)} seconds" if seconds < 120 else f"~{int(seconds // 60)} minutes"
    )


def _base_url() -> str:
    """Externally reachable base, resolved the same way as ``/attachments/{id}``."""
    from core.config import WORKSPACE_MCP_BASE_URI, WORKSPACE_MCP_PORT

    external = os.getenv("WORKSPACE_EXTERNAL_URL")
    return (
        external.rstrip("/")
        if external
        else f"{WORKSPACE_MCP_BASE_URI}:{WORKSPACE_MCP_PORT}"
    )


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
    if filename:
        claims["fn"] = filename
    if mime_type:
        claims["mt"] = mime_type
    payload = json.dumps(claims, separators=(",", ":"), ensure_ascii=False)
    token = Fernet(_signing_key()).encrypt_at_time(payload.encode("utf-8"), now)
    if len(token) > _MAX_TOKEN_CHARS:
        raise ValueError("download token too large for the route to accept")
    return f"{_base_url()}/attachments/signed/{token.decode('ascii')}"


def _is_timestamp(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def verify_token(token: str) -> Optional[dict]:
    """Claims for a valid token, else None (tampered, wrong key, expired,
    malformed, no key). Every check happens before any credential lookup.

    The token must be the canonical URL-safe base64 Fernet emitted (the decoder
    would otherwise ignore bytes appended after the padding). Fernet then
    authenticates it and refuses one older than ``_MAX_TOKEN_AGE_SECONDS`` by its
    own timestamp (the hard ceiling); then the decrypted claims must carry a
    non-empty ``sub`` and integer ``iat``/``exp``, ``exp`` must still be in the
    future and ``iat`` may not lead the clock by more than the skew allowance.
    """
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_CHARS:
        return None
    now = int(time.time())
    try:
        raw = token.encode("ascii")
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


def offer_url(
    user_email: str,
    *,
    source: str,
    ref: dict,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Optional[tuple[str, int]]:
    """Tool-side entry point: ``(url, ttl_seconds)``, or None to use the normal path.

    None when the feature is off, or when the route could not serve the URL: the
    owner's credentials are recoverable from neither store, they are unusable
    (no refresh token and inside google-auth's expiry threshold), or no signing
    key can be derived.
    """
    if not enabled():
        return None
    credentials = _recover_credentials(user_email)
    ttl = clamp_ttl(credentials) if credentials else 0
    if ttl <= 0:
        logger.info(
            "Signed download URL unavailable (credentials not recoverable or not "
            "usable long enough); using the standard download path."
        )
        return None
    try:
        url = mint_url(
            source=source,
            user_email=user_email,
            ref=ref,
            ttl_seconds=ttl,
            filename=filename,
            mime_type=mime_type,
        )
    except (RuntimeError, ValueError) as exc:
        logger.warning("Signed download URL unavailable: %s", exc)
        return None
    return url, ttl


def url_lines(url: str, ttl: int, what: str) -> list[str]:
    """Result lines shared by the tools that hand out a signed URL."""
    return [
        f"\n📎 Download URL: {url}",
        f"\nThe server fetches the {what} directly from Google when this URL is "
        f"requested; the link is signed to you and expires in {format_ttl(ttl)}. "
        "Fetch it promptly; do not queue it for later.",
    ]


UNAVAILABLE_NOTE = (
    "\n⚠️ No signed download URL could be issued: this server could not recover "
    "usable credentials for you (none stored here, or an access token about to "
    "expire with no refresh token). Re-authenticate if this persists."
)


# --- Fetchers: claims + owner credentials -> bytes -------------------------------


@dataclass
class DownloadResult:
    """A buffered body or a bounded-memory stream, never both."""

    filename: str
    media_type: str
    content: Optional[bytes] = None
    stream: Optional[AsyncIterator[bytes]] = None


class SignedDownloadError(Exception):
    """A fetcher could not produce the bytes (served as 502)."""


def _decode_urlsafe(data: str, what: str) -> bytes:
    if not data:
        raise SignedDownloadError(f"{what} has no content")
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
    gmail = build("gmail", "v1", credentials=credentials)
    try:
        attachment = await asyncio.to_thread(
            gmail.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute
        )
    except Exception as exc:
        raise SignedDownloadError(f"Gmail attachment fetch failed: {exc}") from exc
    return DownloadResult(
        filename=claims.get("fn") or "attachment",
        media_type=claims.get("mt") or "application/octet-stream",
        content=_decode_urlsafe(attachment.get("data", ""), "Gmail attachment"),
    )


async def _fetch_gmail_message(
    claims: dict, credentials: Credentials
) -> DownloadResult:
    """A complete message in the export representation the tool offered (``fmt``)."""
    from gmail.gmail_tools import _render_message_export

    message_id, body_format = claims.get("mid"), claims.get("fmt")
    if not message_id or body_format not in ("raw", "html", "text"):
        raise SignedDownloadError("Gmail message token missing mid/fmt")
    gmail = build("gmail", "v1", credentials=credentials)
    try:
        content, mime_type, extension, _notes = await _render_message_export(
            gmail, message_id, body_format
        )
    except Exception as exc:
        raise SignedDownloadError(f"Gmail message export failed: {exc}") from exc
    return DownloadResult(
        filename=claims.get("fn") or f"message{extension}",
        media_type=mime_type,
        content=content,
    )


async def _fetch_drive(claims: dict, credentials: Credentials) -> DownloadResult:
    """Stream a Drive file in bounded chunks; ``emt`` set means export a native file."""
    from gdrive.drive_tools import DOWNLOAD_CHUNK_SIZE, _media_request

    file_id = claims.get("fid")
    if not file_id:
        raise SignedDownloadError("Drive token missing fid")
    export_mime = claims.get("emt")
    drive = build("drive", "v3", credentials=credentials)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buffer,
        _media_request(drive, file_id, export_mime),
        chunksize=DOWNLOAD_CHUNK_SIZE,
    )

    def next_chunk() -> tuple[bytes, bool]:
        # next_chunk() appends to the buffer; drain it so memory stays at one chunk.
        _status, done = downloader.next_chunk()
        chunk = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        return chunk, done

    # Pull the first chunk eagerly so auth / not-found errors become a 502 instead
    # of a truncated 200.
    try:
        chunk, done = await asyncio.to_thread(next_chunk)
    except Exception as exc:
        raise SignedDownloadError(f"Drive download failed: {exc}") from exc

    async def body() -> AsyncIterator[bytes]:
        pending, finished = chunk, done
        while True:
            if pending:
                yield pending
            if finished:
                return
            try:
                pending, finished = await asyncio.to_thread(next_chunk)
            except Exception as exc:  # headers are out; the stream can only end early
                logger.error("Drive stream interrupted mid-download: %s", exc)
                return

    return DownloadResult(
        filename=claims.get("fn") or "download",
        media_type=claims.get("mt") or export_mime or "application/octet-stream",
        stream=body(),
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


def _error(status: int, message: str) -> Response:
    return JSONResponse(
        {"error": message}, status_code=status, headers=_RESPONSE_HEADERS
    )


async def serve(token: str) -> Response:
    """Verify a signed token, then return the resource with its owner's credentials."""
    if not enabled():  # keep the public route inert unless the feature is on
        return _error(404, "Not found")
    claims = verify_token(token)
    fetcher = _FETCHERS.get(claims.get("src", "")) if claims else None
    if not (claims and fetcher and claims.get("sub")):
        return _error(403, "Invalid or expired download link")

    # Only a validly signed token naming this user reaches the lookup.
    credentials = _recover_credentials(claims["sub"])
    if credentials is None or usable_seconds(credentials) <= 0:
        return _error(
            401,
            "The download owner's credentials are not recoverable on this server",
        )
    if not credentials.valid:
        # usable_seconds() > 0 with an invalid token means refreshable. Refresh the
        # in-memory copy only; this public route never writes to any store.
        try:
            await asyncio.to_thread(credentials.refresh, Request())
        except Exception as exc:
            logger.warning("Signed download: credential refresh failed: %s", exc)
            return _error(
                401, "The download owner's credentials could not be refreshed"
            )

    try:
        result = await fetcher(claims, credentials)
    except SignedDownloadError as exc:
        logger.error("Signed download fetch failed: %s", exc)
        return _error(502, "Failed to fetch the requested resource")

    headers = {
        **_RESPONSE_HEADERS,
        "Content-Disposition": _content_disposition(result.filename),
    }
    if result.stream is not None:
        return StreamingResponse(
            result.stream, media_type=result.media_type, headers=headers
        )
    return Response(
        content=result.content, media_type=result.media_type, headers=headers
    )
