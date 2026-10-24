"""
Artifact viewer routes (opt-in).

Two kinds of request reach this server through the gateway:

* the viewer, ``/a/<hash>-<slug>/<path>``, behind sign-in, which checks that the signed-in
  user may open the bundle and returns a shell page holding the sandboxed frame;
* the content, ``/c/<token>/<path>``, on a separate host with no sign-in (a sandboxed
  frame cannot send cookies), authenticated only by the expiring token in the path.

The gateway adds one marker header to each route so this process can tell them apart.
"""

import asyncio
import html
import json
import logging
import mimetypes
import re
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from auth.gateway_identity import extract_email_from_assertion
from auth.google_auth import build_google_service, get_credentials, recycling
from auth.oauth_config import get_oauth_config
from auth.scopes import DRIVE_READONLY_SCOPE
from gartifacts.artifacts_config import (
    APP_MARKER,
    CONTENT_MARKER,
    ROUTE_MARKER_HEADER,
    content_base_url,
    icon,
    viewer_base_url,
)
from gartifacts.artifacts_consent import begin_consent
from gartifacts.artifacts_drive import (
    AmbiguousNameError,
    NotFoundError,
    NotServableError,
    get_folder,
    resolve_path,
)
from gartifacts.artifacts_links import (
    build_link_segment,
    parse_link_segment,
    title_from_folder_name,
)
from gartifacts.artifacts_policy import (
    ArtifactPolicy,
    build_csp,
    load_policy,
)
from gartifacts.artifacts_registry import get_artifact_registry
from gartifacts.artifacts_shell import ICON_ROUTE, inject_tracker, shell_page
from gartifacts.artifacts_tokens import (
    TokenConfigError,
    mint_content_token,
    verify_content_token,
)

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("gartifacts.access")

# Requests that load a resource into a page. Navigations are allowed only into a frame:
# a content URL opened on its own would show artifact script with no banner.
SUBRESOURCE_DESTS = frozenset(
    {
        "script",
        "style",
        "image",
        "font",
        "empty",
        "worker",
        "sharedworker",
        "serviceworker",
        "audio",
        "video",
        "track",
        "manifest",
        "report",
        "xslt",
    }
)

CONTENT_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
}
_DOCUMENT_TYPES = ("text/html", "image/svg+xml")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_FOLDER_OK_TTL = 30.0
_REPORT_MAX_BYTES = 8192
_REPORT_PER_MINUTE = 120


class NeedsConsent(Exception):
    """The user has no usable Google grant for Drive read."""


class Denied(Exception):
    """Drive refused access, or the folder is gone."""


def log_event(event: str, **fields: Any) -> None:
    """One structured access-log line. Never include tokens or file contents."""
    access_logger.info(json.dumps({"event": event, **fields}, sort_keys=True))


def _page(
    status: int, title: str, message: str, headers: Optional[Dict[str, str]] = None
) -> HTMLResponse:
    body = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        f"<title>{html.escape(title)}</title>"
        '<body style="font:16px system-ui;margin:3rem auto;max-width:34rem">'
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )
    merged = {
        **_SECURITY_HEADERS,
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
        "frame-ancestors 'none'",
        **(headers or {}),
    }
    return HTMLResponse(body, status_code=status, headers=merged)


def _unavailable() -> HTMLResponse:
    return _page(
        404,
        "Not available",
        "This artifact does not exist, or you do not have access to it.",
    )


def _marker(request: Request) -> str:
    return request.headers.get(ROUTE_MARKER_HEADER, "").strip().lower()


async def _principal(request: Request) -> Optional[str]:
    """The verified email from the gateway's signed assertion, or None."""
    config = get_oauth_config()
    if not config.trust_gateway_identity:
        return None
    token = request.headers.get(config.gateway_identity_header)
    if not token:
        return None
    return await asyncio.to_thread(extract_email_from_assertion, token)


async def _drive_service(email: str):
    """Authorised Drive client for ``email`` or raise NeedsConsent."""
    credentials = await asyncio.to_thread(
        get_credentials, email, [DRIVE_READONLY_SCOPE], None
    )
    if credentials is None:
        raise NeedsConsent()
    return build_google_service("drive", "v3", credentials)


def _classify(exc: Exception) -> Exception:
    """Map a Drive/auth failure to NeedsConsent or Denied; anything else is kept."""
    if isinstance(exc, RefreshError):
        return NeedsConsent()
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", 0)
        if status in (401,):
            return NeedsConsent()
        if status in (403, 404):
            return Denied()
    return exc


_folder_ok: Dict[Tuple[str, str], float] = {}
_folder_ok_lock = threading.Lock()


async def _check_folder(
    service: Any, email: str, folder_id: str, *, force: bool = False
) -> Dict[str, Any]:
    """Confirm the registered folder is readable by the user and not in the trash."""
    key = (email, folder_id)
    now = time.monotonic()
    if not force:
        with _folder_ok_lock:
            if _folder_ok.get(key, 0.0) > now:
                return {}
    try:
        meta = await get_folder(service, folder_id)
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc) from exc
    if (
        meta.get("trashed")
        or meta.get("mimeType") != "application/vnd.google-apps.folder"
    ):
        raise Denied()
    with _folder_ok_lock:
        if len(_folder_ok) > 4096:
            _folder_ok.clear()
        _folder_ok[key] = now + _FOLDER_OK_TTL
    return meta


async def viewer_page(request: Request) -> Response:
    """``GET /a/<hash>-<slug>/<path>``: authorise the viewer and return the shell."""
    request_id = uuid.uuid4().hex[:12]
    if _marker(request) != APP_MARKER:
        return _unavailable()
    email = await _principal(request)
    if not email:
        return _page(
            401, "Sign-in required", "Open this link through the company gateway."
        )
    segment = request.path_params.get("segment", "")
    path = request.path_params.get("path", "")
    artifact_hash = parse_link_segment(segment)
    registry = get_artifact_registry()
    record = (
        await asyncio.to_thread(registry.get_by_hash, artifact_hash)
        if artifact_hash
        else None
    )
    if record is None:
        log_event(
            "view",
            user=email,
            hash=artifact_hash,
            outcome="unregistered",
            request_id=request_id,
        )
        return _unavailable()

    here = f"/a/{segment}/{quote(path)}"
    content_origin = content_base_url()
    if not content_origin:
        logger.error("WORKSPACE_MCP_ARTIFACTS_CONTENT_URL is not set")
        return _page(
            500, "Not configured", "The artifact viewer is not fully configured."
        )

    try:
        service = await _drive_service(email)
        with recycling(service) as drive:
            meta = await _check_folder(drive, email, record.folder_id, force=True)
    except NeedsConsent:
        try:
            url = await asyncio.to_thread(begin_consent, email, here)
        except Exception:
            logger.exception("Could not start Google consent")
            return _page(
                500, "Sign-in failed", "Could not start Google sign-in. Try again."
            )
        log_event(
            "view",
            user=email,
            hash=record.hash,
            outcome="consent",
            request_id=request_id,
        )
        return RedirectResponse(
            url, status_code=302, headers={"Cache-Control": "no-store"}
        )
    except Denied:
        log_event(
            "view",
            user=email,
            hash=record.hash,
            outcome="denied_or_missing",
            request_id=request_id,
        )
        return _unavailable()
    except Exception:
        logger.exception("Drive check failed")
        return _page(
            502, "Try again", "Google Drive could not be reached. Try again shortly."
        )

    title = title_from_folder_name(meta.get("name", "")) or record.title
    if title != record.title:
        record = await asyncio.to_thread(registry.refresh, record.hash, title) or record
    canonical = build_link_segment(record.hash, record.slug)
    if segment != canonical:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(
            f"/a/{canonical}/{quote(path)}{query}",
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
    try:
        token = mint_content_token(record.hash, email)
    except TokenConfigError:
        logger.error("Artifact token key is not configured")
        return _page(
            500, "Not configured", "The artifact viewer is not fully configured."
        )
    log_event(
        "view", user=email, hash=record.hash, outcome="allowed", request_id=request_id
    )
    return shell_page(
        record.title,
        f"{content_origin}/c/{token}/{quote(path)}",
        content_origin,
        f"/a/{canonical}/{quote(path)}",
        f"/a/{canonical}",
        _SECURITY_HEADERS,
    )


def _content_headers(policy: ArtifactPolicy) -> Dict[str, str]:
    app_origin = viewer_base_url()
    return {
        **_SECURITY_HEADERS,
        "Content-Security-Policy": build_csp(
            policy, frame_ancestors=app_origin, report_path="/csp-report"
        ),
        # The sandboxed frame has an opaque origin, so its module scripts, fonts and
        # fetches arrive cross-origin; the path token, not the origin, is the credential.
        "Access-Control-Allow-Origin": "*",
        "Cross-Origin-Resource-Policy": "cross-origin",
    }


def _content_type(name: str) -> Tuple[str, bool]:
    """Return (content type, forced download) from the file extension only."""
    lowered = name.lower()
    for extension, content_type in CONTENT_TYPES.items():
        if lowered.endswith(extension):
            return content_type, False
    guessed, _ = mimetypes.guess_type(lowered)
    if guessed and guessed.startswith(("image/", "audio/", "video/", "font/")):
        return guessed, False
    return "application/octet-stream", True


async def content_file(request: Request) -> Response:
    """``GET /c/<token>/<path>``: serve one file from a registered bundle."""
    if _marker(request) != CONTENT_MARKER:
        return _unavailable()
    policy = load_policy()
    headers = _content_headers(policy)
    claims = verify_content_token(request.path_params.get("token", ""))
    if claims is None:
        return _page(
            403,
            "This view has expired",
            "Use the Reload button on the viewer page to continue.",
            headers,
        )
    registry = get_artifact_registry()
    record = await asyncio.to_thread(registry.get_by_hash, claims.artifact_hash)
    if record is None:
        return _unavailable()
    path = request.path_params.get("path", "")

    dest = request.headers.get("sec-fetch-dest", "").strip().lower()
    if dest not in SUBRESOURCE_DESTS and dest != "iframe":
        log_event(
            "unframed", user=claims.email, hash=record.hash, dest=dest or "missing"
        )
        base = viewer_base_url()
        if base:
            target = (
                f"{base}/a/{build_link_segment(record.hash, record.slug)}/{quote(path)}"
            )
            return RedirectResponse(
                target, status_code=302, headers={"Cache-Control": "no-store"}
            )
        return _page(
            403,
            "Open this through the viewer",
            "This page cannot be opened on its own.",
            headers,
        )

    try:
        service = await _drive_service(claims.email)
        with recycling(service) as drive:
            await _check_folder(drive, claims.email, record.folder_id)
            item = await resolve_path(drive, record.folder_id, path)
            size = int(item.get("size") or 0)
            if size > policy.limits["max_file_bytes"]:
                return _page(
                    413, "File too large", "This file is over the size limit.", headers
                )
            data = await asyncio.to_thread(
                drive.files().get_media(fileId=item["id"]).execute
            )
    except NeedsConsent:
        return _page(
            401, "Sign-in expired", "Reload the viewer page to continue.", headers
        )
    except (Denied, NotFoundError, NotServableError, AmbiguousNameError):
        log_event("content", user=claims.email, hash=record.hash, outcome="not_found")
        return _page(404, "Not found", "That file is not available.", headers)
    except Exception as exc:  # noqa: BLE001
        mapped = _classify(exc)
        if isinstance(mapped, NeedsConsent):
            return _page(
                401, "Sign-in expired", "Reload the viewer page to continue.", headers
            )
        if isinstance(mapped, Denied):
            return _unavailable()
        logger.exception("Content fetch failed")
        return _page(502, "Try again", "Google Drive could not be reached.", headers)

    content_type, download = _content_type(item.get("name", ""))
    out = {**headers}
    if download:
        out["Content-Disposition"] = "attachment"
    body = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
    if content_type.startswith("text/html"):
        body = inject_tracker(bytes(body), viewer_base_url())
    log_event(
        "content",
        user=claims.email,
        hash=record.hash,
        outcome="served",
        bytes=len(body),
    )
    return Response(bytes(body), media_type=content_type, headers=out)


async def content_no_slash(request: Request) -> Response:
    """``/c/<token>`` without a slash: relative links need the trailing one."""
    return RedirectResponse(f"/c/{request.path_params['token']}/", status_code=308)


_report_times: list = []
_report_lock = threading.Lock()


_TOKEN_IN_PATH = re.compile(r"/c/[^/\s?#]+")


def _scrub(value: Any) -> Any:
    """Remove path tokens from report fields before they are logged."""
    if isinstance(value, str):
        return _TOKEN_IN_PATH.sub("/c/<token>", value)[:500]
    return value


async def csp_report(request: Request) -> Response:
    """``POST /csp-report``: log a policy violation, scrubbed, with a global rate limit."""
    if _marker(request) != CONTENT_MARKER:
        return Response(status_code=404)
    now = time.monotonic()
    with _report_lock:
        _report_times[:] = [t for t in _report_times if now - t < 60]
        if len(_report_times) >= _REPORT_PER_MINUTE:
            return Response(status_code=204)
        _report_times.append(now)
    body = await request.body()
    if len(body) <= _REPORT_MAX_BYTES:
        try:
            data = json.loads(body)
            report = data.get("csp-report", data) if isinstance(data, dict) else {}
            fields = {
                key: _scrub(report.get(key))
                for key in ("document-uri", "violated-directive", "blocked-uri")
                if isinstance(report, dict)
            }
            log_event("csp_violation", **fields)
        except ValueError:
            pass
    return Response(status_code=204)


async def viewer_icon(request: Request) -> Response:
    """``GET /a/_icon``: the configured favicon and header logo, if any."""
    if _marker(request) != APP_MARKER:
        return _unavailable()
    found = icon()
    if found is None:
        return Response(status_code=404)
    data, content_type = found
    return Response(
        data,
        media_type=content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=3600",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


async def viewer_no_slash(request: Request) -> Response:
    """``/a/<segment>`` without a slash: relative links need the trailing one."""
    return RedirectResponse(f"/a/{request.path_params['segment']}/", status_code=308)


def viewer_routes() -> list:
    """Starlette routes for the viewer, content and report endpoints."""
    return [
        Route(ICON_ROUTE, viewer_icon, methods=["GET"]),
        Route("/a/{segment}/{path:path}", viewer_page, methods=["GET"]),
        Route("/a/{segment}", viewer_no_slash, methods=["GET"]),
        Route("/c/{token}/{path:path}", content_file, methods=["GET"]),
        Route("/c/{token}", content_no_slash, methods=["GET"]),
        Route("/csp-report", csp_report, methods=["POST"]),
    ]


def register_viewer_routes(server: Any) -> None:
    """Attach the routes to a FastMCP server (called only when the viewer is enabled)."""
    for route in viewer_routes():
        server.custom_route(route.path, methods=sorted(route.methods or []))(
            route.endpoint
        )
