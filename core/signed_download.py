"""Per-source fetchers for signed download URLs (Design A).

The ``/attachments/signed`` route is source-agnostic: it verifies the token,
recovers the owner's Google credentials, then hands the claims off to the fetcher
registered for ``claims["src"]``. Each fetcher turns the source-specific claims +
credentials into ``(bytes, filename, mime_type)``; the route streams the result.

Adding a new download source = add a fetcher and register it. Nothing else in the
route changes.
"""

import asyncio
import base64
import binascii
import io
import logging
import os
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

_DEFAULT_DRIVE_CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB


def _resolve_drive_chunk_size() -> int:
    """Bytes pulled from Drive per range request.

    Tunable via WORKSPACE_MCP_DRIVE_STREAM_CHUNK_BYTES to trade per-download memory
    against round-trips (bigger = fewer requests/faster, more RAM per concurrent
    download). The googleapiclient default is 100MB, which would buffer 100MB at a
    time — defeating the point — so we always set our own.
    """
    raw = os.getenv("WORKSPACE_MCP_DRIVE_STREAM_CHUNK_BYTES", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
            logger.warning(
                "WORKSPACE_MCP_DRIVE_STREAM_CHUNK_BYTES must be positive; got %r. "
                "Using default %d.",
                raw,
                _DEFAULT_DRIVE_CHUNK_SIZE,
            )
        except ValueError:
            logger.warning(
                "Invalid WORKSPACE_MCP_DRIVE_STREAM_CHUNK_BYTES=%r; using default %d.",
                raw,
                _DEFAULT_DRIVE_CHUNK_SIZE,
            )
    return _DEFAULT_DRIVE_CHUNK_SIZE


_DRIVE_CHUNK_SIZE = _resolve_drive_chunk_size()


@dataclass
class DownloadResult:
    """What a fetcher returns: a buffered body or a bounded-memory stream.

    A source sets exactly one of ``content`` / ``stream``. Gmail attachments arrive
    whole in a single API response (and are size-bounded by the email limit), so
    they're buffered. Drive downloads chunk via MediaIoBaseDownload, so they stream —
    a multi-GB file never sits in RAM all at once.
    """

    filename: str
    media_type: str
    content: Optional[bytes] = None
    stream: Optional[AsyncIterator[bytes]] = None


class SignedDownloadError(Exception):
    """Raised when a fetcher cannot produce the bytes (maps to a 502 in the route)."""


async def _fetch_gmail(claims: dict, credentials: Credentials) -> DownloadResult:
    """Fetch a Gmail attachment by message + attachment id.

    Gmail attachment ids are ephemeral and rotate between fetches, so the filename
    is resolved here (after the bytes are in hand) by matching the message payload
    on byte size — the stable key, matching the download tool's own fallback.
    """
    from gmail.gmail_tools import _extract_attachments

    message_id = claims.get("mid")
    attachment_id = claims.get("aid")
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

    data = attachment.get("data", "")
    if not data:
        raise SignedDownloadError("Gmail attachment has no content")

    # Gmail returns URL-safe base64; pad before decoding.
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SignedDownloadError(f"Gmail attachment decode failed: {exc}") from exc

    filename = claims.get("fn") or "attachment"
    media_type = claims.get("mt") or "application/octet-stream"
    if not claims.get("fn"):
        try:
            meta = await asyncio.to_thread(
                gmail.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="full",
                    fields="payload(parts(filename,mimeType,body(attachmentId,size)),body(attachmentId,size),filename,mimeType)",
                )
                .execute
            )
            atts = _extract_attachments(meta.get("payload", {}))

            chosen = None
            for att in atts:  # exact id first (covers the not-yet-rotated case)
                if att.get("attachmentId") == attachment_id:
                    chosen = att
                    break
            if chosen is None:  # else the attachment whose size matches the bytes
                size_matches = [
                    att
                    for att in atts
                    if att.get("size") and abs(att["size"] - len(raw)) < 100
                ]
                if len(size_matches) == 1:
                    chosen = size_matches[0]
            if chosen is None and len(atts) == 1:  # else the only attachment
                chosen = atts[0]

            if chosen:
                filename = chosen.get("filename") or filename
                media_type = chosen.get("mimeType") or media_type
        except Exception:
            logger.debug("Could not resolve Gmail filename; using defaults")

    # Gmail's whole attachment is already in memory (single API response), so buffer.
    return DownloadResult(filename=filename, media_type=media_type, content=raw)


async def _fetch_drive(claims: dict, credentials: Credentials) -> DownloadResult:
    """Fetch a Drive file by id, exporting native Google files when ``emt`` is set.

    Drive file ids are stable, so filename/MIME are resolved at mint time and signed
    into the token (``fn``/``mt``). The bytes are streamed in bounded chunks so a
    large file never sits in RAM all at once.

    The first chunk is pulled eagerly so auth / not-found failures surface as a 502
    before the streaming response starts; later chunks stream as they download.
    """
    file_id = claims.get("fid")
    if not file_id:
        raise SignedDownloadError("Drive token missing fid")
    export_mime = claims.get("emt")  # set only for native Google file exports

    drive = build("drive", "v3", credentials=credentials)
    # supportsAllDrives is required for shared-drive files (the API 404s without
    # it); export_media doesn't take it, matching the tool-side download path.
    request_obj = (
        drive.files().export_media(fileId=file_id, mimeType=export_mime)
        if export_mime
        else drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    )

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request_obj, chunksize=_DRIVE_CHUNK_SIZE)

    def _next_chunk() -> tuple[bytes, bool]:
        # next_chunk() appends one chunk to fh (no seek); read it out, then reset fh
        # so memory stays bounded to one chunk. Safe: MediaIoBaseDownload tracks its
        # position via Range headers, not the file handle's position.
        _status, done = downloader.next_chunk()
        chunk = fh.getvalue()
        fh.seek(0)
        fh.truncate(0)
        return chunk, done

    try:
        first_chunk, done = await asyncio.to_thread(_next_chunk)
    except Exception as exc:
        raise SignedDownloadError(f"Drive download failed: {exc}") from exc

    async def body() -> AsyncIterator[bytes]:
        chunk, finished = first_chunk, done
        if chunk:
            yield chunk
        while not finished:
            try:
                chunk, finished = await asyncio.to_thread(_next_chunk)
            except Exception as exc:
                # Headers are already sent; we can only truncate the stream.
                logger.error("Drive stream interrupted mid-download: %s", exc)
                return
            if chunk:
                yield chunk

    filename = claims.get("fn") or "download"
    media_type = claims.get("mt") or export_mime or "application/octet-stream"
    return DownloadResult(filename=filename, media_type=media_type, stream=body())


async def _fetch_gmail_message(claims: dict, credentials: Credentials) -> DownloadResult:
    """Fetch a COMPLETE Gmail message (not an attachment) by message id.

    ``fmt`` selects the representation: ``eml`` = the raw RFC 5322 message (byte-exact,
    all headers/parts/inline attachments), ``html`` = the raw HTML body, ``txt`` =
    plaintext (HTML converted to text as fallback). The message id is stable, so the
    filename/MIME are signed into the token (``fn``/``mt``). The whole message arrives
    in a single API response, so the result is buffered.
    """
    from gmail.gmail_tools import _extract_message_bodies, _html_to_text

    message_id = claims.get("mid")
    fmt = claims.get("fmt") or "eml"
    if not message_id:
        raise SignedDownloadError("Gmail message token missing mid")

    gmail = build("gmail", "v1", credentials=credentials)

    if fmt == "eml":
        try:
            msg = await asyncio.to_thread(
                gmail.users()
                .messages()
                .get(userId="me", id=message_id, format="raw")
                .execute
            )
        except Exception as exc:
            raise SignedDownloadError(f"Gmail message fetch failed: {exc}") from exc
        raw = msg.get("raw", "")
        if not raw:
            raise SignedDownloadError("Gmail message has no raw content")
        try:
            content = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except (binascii.Error, ValueError) as exc:
            raise SignedDownloadError(f"Gmail message decode failed: {exc}") from exc
        default_mt = "message/rfc822"
    else:
        try:
            msg = await asyncio.to_thread(
                gmail.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute
            )
        except Exception as exc:
            raise SignedDownloadError(f"Gmail message fetch failed: {exc}") from exc
        bodies = _extract_message_bodies(msg.get("payload", {}))
        text_stripped = bodies.get("text", "").strip()
        html_stripped = bodies.get("html", "").strip()
        if fmt == "html":
            body = html_stripped or text_stripped
            default_mt = "text/html"
        else:  # txt
            if text_stripped:
                body = text_stripped
            elif html_stripped:
                body = _html_to_text(html_stripped).strip()
            else:
                body = ""
            default_mt = "text/plain"
        if not body:
            raise SignedDownloadError("Gmail message has no readable body content")
        content = body.encode("utf-8")

    filename = claims.get("fn") or f"message.{fmt}"
    media_type = claims.get("mt") or default_mt
    return DownloadResult(filename=filename, media_type=media_type, content=content)


_FETCHERS: dict[str, Callable[[dict, Credentials], Awaitable[DownloadResult]]] = {
    "gmail": _fetch_gmail,
    "gmail_message": _fetch_gmail_message,
    "drive": _fetch_drive,
}


def get_fetcher(
    source: str,
) -> Optional[Callable[[dict, Credentials], Awaitable[DownloadResult]]]:
    """Return the fetcher for a token source, or None if unsupported."""
    return _FETCHERS.get(source)
