"""
Switches for the optional artifacts feature.

Everything artifact-related is off unless explicitly enabled, so a server that sets none
of these variables behaves exactly as it did before the feature existed.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import yaml

logger = logging.getLogger(__name__)

# Adds the artifacts tools to the default service set (they can also be selected
# explicitly with --tools artifacts or --permissions artifacts:readonly).
ARTIFACTS_ENV = "WORKSPACE_MCP_ARTIFACTS"
# Mounts the artifact viewer and content routes.
VIEWER_ENV = "WORKSPACE_MCP_ARTIFACTS_VIEWER"
# Public base URL of the viewer, used to build links returned to users.
VIEWER_URL_ENV = "WORKSPACE_MCP_ARTIFACTS_VIEWER_URL"
# Public base URL of the separate host that serves artifact content.
CONTENT_URL_ENV = "WORKSPACE_MCP_ARTIFACTS_CONTENT_URL"

# Optional YAML or JSON file that sets the viewer's branding and wording (see
# ``Branding`` for the keys). Unset means the neutral defaults.
BRANDING_FILE_ENV = "WORKSPACE_MCP_ARTIFACTS_BRANDING_FILE"

# The gateway tells the two hosts apart with one request header (the Host header is
# rewritten on the way in): "app" on the viewer route, "content" on the content route.
ROUTE_MARKER_HEADER = "x-artifacts-route"
APP_MARKER = "app"
CONTENT_MARKER = "content"

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE


def artifacts_enabled() -> bool:
    """True when the artifacts tools should load by default."""
    return _flag(ARTIFACTS_ENV)


def viewer_enabled() -> bool:
    """True when the viewer and content routes should be mounted."""
    return _flag(VIEWER_ENV)


def _origin_from_env(name: str) -> Optional[str]:
    raw = os.getenv(name, "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.path or parts.query or parts.fragment:
        return None
    return raw


def viewer_base_url() -> Optional[str]:
    """The viewer's public base URL without a trailing slash, or None if unset/invalid."""
    return _origin_from_env(VIEWER_URL_ENV)


def content_base_url() -> Optional[str]:
    """The content host's public base URL without a trailing slash, or None."""
    return _origin_from_env(CONTENT_URL_ENV)


DEFAULT_NAME = "Artifacts"
DEFAULT_COLOR = "#1f3a5f"
DEFAULT_NOTICE_DAYS = 30
DEFAULT_NOTICE = (
    "This content was created by people (or by AI) and has not been reviewed.",
    "Don't sign in to any site here. A login box in this area could be a fake.",
    "Only the bar at the top is part of the viewer.",
)
DEFAULT_ABOUT = (
    "Artifacts are made by colleagues with Claude and are meant for sharing inside "
    "the company.",
    "Only people with access to the Drive folder can open one.",
    "To share outside the company, use the PDF button and check the file first.",
    "Anyone you share an artifact's folder with can see everything in it.",
)
DEFAULT_REQUEST_MESSAGE = (
    "Viewer access to artifact: {url} - please copy the artifact url into the "
    "message window if you approve this request"
)
DEFAULT_REQUEST_STEPS = (
    "Click <strong>Request access in Google Drive</strong> below (it opens in a new tab).",
    "On Google's page, choose <strong>Viewer</strong>. Google preselects Editor, "
    "which would let the person who approves you hand over edit rights.",
    "Paste the message below into Google's message box, then send the request.",
    "When the owner approves, come back to this page and press "
    "<strong>Check again</strong>. Google's approval email links to the Drive folder; "
    "this page is the viewer.",
)
_ICON_MAX_BYTES = 256 * 1024
_ICON_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
_MAX_ITEMS = 12
_MAX_TEXT = 2000


@dataclass(frozen=True)
class Branding:
    """What the viewer shows around an artifact.

    ``notice`` and ``about`` are lists of HTML fragments. The branding file is trusted
    configuration, so they are inserted as written; the page's script policy still
    refuses inline script. ``notice_days`` is how long an acknowledged notice stays
    dismissed (0 turns the first-visit modal off).
    """

    name: str = DEFAULT_NAME
    color: str = DEFAULT_COLOR
    icon: Optional[Tuple[bytes, str]] = None
    notice_days: int = DEFAULT_NOTICE_DAYS
    notice: Tuple[str, ...] = DEFAULT_NOTICE
    about: Tuple[str, ...] = DEFAULT_ABOUT
    request_message: str = DEFAULT_REQUEST_MESSAGE
    request_steps: Tuple[str, ...] = DEFAULT_REQUEST_STEPS


def _text_list(value: Any, fallback: Tuple[str, ...]) -> Tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        return fallback
    items = [v for v in value if isinstance(v, str) and 0 < len(v) <= _MAX_TEXT]
    return tuple(items) if items and len(items) == len(value) else fallback


def _message(value: Any) -> str:
    """The text people paste into Google's request box (``{url}`` is the viewer link)."""
    if isinstance(value, str) and "{url}" in value and len(value) <= 220:
        return value.strip()
    return DEFAULT_REQUEST_MESSAGE


def _load_icon(raw: Any, base_dir: str) -> Optional[Tuple[bytes, str]]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = raw if os.path.isabs(raw) else os.path.join(base_dir, raw)
    content_type = _ICON_TYPES.get(os.path.splitext(path)[1].lower())
    if content_type is None:
        logger.error("Artifact branding: unsupported icon type %s", path)
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(_ICON_MAX_BYTES + 1)
    except OSError as exc:
        logger.error("Artifact branding: cannot read icon %s: %s", path, exc)
        return None
    if not data or len(data) > _ICON_MAX_BYTES:
        logger.error("Artifact branding: icon %s is empty or too large", path)
        return None
    return data, content_type


def _parse_branding(path: str) -> Branding:
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("the branding file must hold a mapping")
    name = data.get("name")
    color = data.get("color")
    days = data.get("notice_days")
    return Branding(
        name=name.strip()[:60]
        if isinstance(name, str) and name.strip()
        else DEFAULT_NAME,
        color=color.strip()
        if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color.strip())
        else DEFAULT_COLOR,
        icon=_load_icon(data.get("icon"), os.path.dirname(os.path.abspath(path))),
        notice_days=days
        if isinstance(days, int) and not isinstance(days, bool) and 0 <= days <= 365
        else DEFAULT_NOTICE_DAYS,
        notice=_text_list(data.get("notice"), DEFAULT_NOTICE),
        about=_text_list(data.get("about"), DEFAULT_ABOUT),
        request_message=_message(data.get("request_message")),
        request_steps=_text_list(data.get("request_steps"), DEFAULT_REQUEST_STEPS),
    )


_branding_cache: Dict[str, Tuple[float, Branding]] = {}


def branding() -> Branding:
    """The branding in effect: the configured file (re-read when it changes) or defaults."""
    path = os.getenv(BRANDING_FILE_ENV, "").strip()
    if not path:
        return Branding()
    try:
        mtime = os.stat(path).st_mtime
        cached = _branding_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        result = _parse_branding(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.error("Artifact branding file %s is unusable: %s", path, exc)
        return Branding()
    _branding_cache[path] = (mtime, result)
    return result
