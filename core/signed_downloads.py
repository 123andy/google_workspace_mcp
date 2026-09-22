"""Signed, short-lived download URLs that stream Google content on demand.

Neither Gmail nor Drive offers a public download URL: bytes only come back from
an authenticated API call. Without this module a remote server must either write
the file to local disk and serve it from ``/attachments/{id}`` (impossible in
stateless mode, and not tied to a user) or hand base64 back through the model.

With ``WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS=true`` the download tools instead
return ``/attachments/signed/{token}``: an HS256 JWT naming the resource, its
owner (``sub``) and an expiry. The route verifies the signature, recovers the
owner's credentials from the in-process session store, fetches from Google and
streams to the client. The signature is the authorization; nothing is stored.

A URL is clamped to the owner's access-token life (the route cannot refresh a
token the OAuth proxy holds) and is only minted when the route will be able to
serve it. Single-process: the verifying process must hold the owner's session.
"""

import asyncio
import base64
import binascii
import functools
import io
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import quote

import jwt
from fastapi.responses import JSONResponse, Response, StreamingResponse
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from core.config import get_transport_mode

logger = logging.getLogger(__name__)

_ALG = "HS256"
_KEY_SALT = "workspace-mcp-signed-download"
URL_TTL_SECONDS = 900
# A URL must expire at least this long before the credential it depends on.
_EXPIRY_MARGIN_SECONDS = 30
_RESERVED_CLAIMS = frozenset({"src", "sub", "iat", "exp", "fn", "mt"})


def enabled() -> bool:
    """Opt-in, and only over streamable-http: the stdio callback server does not
    mount this route, and a local server can hand out file paths instead."""
    return (
        os.getenv("WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS", "false").lower() == "true"
        and get_transport_mode() == "streamable-http"
    )


@functools.lru_cache(maxsize=1)
def _signing_key() -> bytes:
    """Derive the HMAC key from the OAuth proxy's key material under a dedicated salt."""
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


def clamp_ttl(expiry: Optional[datetime], *, now: Optional[datetime] = None) -> int:
    """Lifetime for a URL that must die before its credential; <= 0 means do not mint.

    ``expiry`` is google-auth's naive-UTC ``Credentials.expiry``. Deliberately not
    floored: a minimum would let a URL outlive the token that serves it.
    """
    if expiry is None:
        return URL_TTL_SECONDS
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    left = (expiry.replace(tzinfo=timezone.utc) - ref).total_seconds()
    return int(min(URL_TTL_SECONDS, left - _EXPIRY_MARGIN_SECONDS))


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
    """Sign a capability URL for one resource (``ref``: fetcher-specific locator) and owner."""
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
    token = jwt.encode(claims, _signing_key(), algorithm=_ALG)
    return f"{_base_url()}/attachments/signed/{token}"


def verify_token(token: str) -> Optional[dict]:
    """Claims for a valid token, else None (bad signature, expired, malformed, no key)."""
    try:
        return jwt.decode(
            token,
            _signing_key(),
            algorithms=[_ALG],
            options={"require": ["exp", "sub", "iat"]},
        )
    except Exception:
        return None


def _session_credentials(user_email: str) -> Optional[Credentials]:
    from auth.oauth21_session_store import get_oauth21_session_store

    try:
        return get_oauth21_session_store().get_credentials(user_email)
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
    owner's credentials are not in the session store the route consults, the
    token is too near expiry, or no signing key can be derived.
    """
    if not enabled():
        return None
    credentials = _session_credentials(user_email)
    ttl = clamp_ttl(credentials.expiry) if credentials else 0
    if ttl <= 0:
        logger.info(
            "Signed download URL unavailable (credentials not recoverable or token "
            "near expiry); using the standard download path."
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
        f"\nThe server streams the {what} directly from Google when this URL is "
        f"fetched; the link is signed to you and expires in {format_ttl(ttl)}. "
        "Fetch it promptly; do not queue it for later.",
    ]


UNAVAILABLE_NOTE = (
    "\n⚠️ No signed download URL could be issued: the stored credentials were not "
    "recoverable or the OAuth token is too near expiry. Re-authenticate to restore "
    "signed download URLs."
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
    """Names come from Gmail/Drive metadata: strip header-breaking characters."""
    ascii_name = (
        name.encode("ascii", "ignore")
        .decode()
        .translate(str.maketrans("", "", '"\\\r\n'))
        or "download"
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


async def serve(token: str) -> Response:
    """Verify a signed token, then stream the resource with its owner's credentials."""
    if not enabled():  # keep the public route inert unless the feature is on
        return JSONResponse({"error": "Not found"}, status_code=404)
    claims = verify_token(token)
    fetcher = _FETCHERS.get(claims.get("src", "")) if claims else None
    if not (claims and fetcher and claims.get("sub")):
        return JSONResponse(
            {"error": "Invalid or expired download link"}, status_code=403
        )

    credentials = _session_credentials(claims["sub"])
    if credentials is None or not credentials.valid:
        # The OAuth proxy holds the refresh token, so an expired or missing access
        # token cannot be renewed here; fail closed.
        return JSONResponse(
            {"error": "The download owner's session is not available; re-authenticate"},
            status_code=401,
        )

    try:
        result = await fetcher(claims, credentials)
    except SignedDownloadError as exc:
        logger.error("Signed download fetch failed: %s", exc)
        return JSONResponse(
            {"error": "Failed to fetch the requested resource"}, status_code=502
        )

    headers = {"Content-Disposition": _content_disposition(result.filename)}
    if result.stream is not None:
        return StreamingResponse(
            result.stream, media_type=result.media_type, headers=headers
        )
    return Response(
        content=result.content, media_type=result.media_type, headers=headers
    )
