"""
Switches for the optional artifacts feature.

Everything artifact-related is off unless explicitly enabled, so a server that sets none
of these variables behaves exactly as it did before the feature existed.
"""

import os
import re
from typing import Optional, Tuple
from urllib.parse import urlsplit

# Adds the artifacts tools to the default service set (they can also be selected
# explicitly with --tools artifacts or --permissions artifacts:readonly).
ARTIFACTS_ENV = "WORKSPACE_MCP_ARTIFACTS"
# Mounts the artifact viewer and content routes.
VIEWER_ENV = "WORKSPACE_MCP_ARTIFACTS_VIEWER"
# Public base URL of the viewer, used to build links returned to users.
VIEWER_URL_ENV = "WORKSPACE_MCP_ARTIFACTS_VIEWER_URL"
# Public base URL of the separate host that serves artifact content.
CONTENT_URL_ENV = "WORKSPACE_MCP_ARTIFACTS_CONTENT_URL"

# Branding of the viewer's header bar. All optional; the defaults are neutral.
BRAND_NAME_ENV = "WORKSPACE_MCP_ARTIFACTS_BRAND_NAME"
BRAND_COLOR_ENV = "WORKSPACE_MCP_ARTIFACTS_BRAND_COLOR"
# Path of an image file (PNG, SVG or ICO) used as the favicon and the header logo.
ICON_PATH_ENV = "WORKSPACE_MCP_ARTIFACTS_ICON_PATH"

DEFAULT_BRAND_NAME = "Artifacts"
DEFAULT_BRAND_COLOR = "#1f3a5f"
_ICON_MAX_BYTES = 256 * 1024
_ICON_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

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


def brand_name() -> str:
    """Name shown in the header bar."""
    return os.getenv(BRAND_NAME_ENV, "").strip()[:60] or DEFAULT_BRAND_NAME


def brand_color() -> str:
    """Header background as ``#rrggbb``; anything else falls back to the default."""
    value = os.getenv(BRAND_COLOR_ENV, "").strip()
    return value if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else DEFAULT_BRAND_COLOR


_icon_cache: dict = {}


def icon() -> Optional[Tuple[bytes, str]]:
    """The configured icon as (bytes, content type), or None when unset or unusable."""
    path = os.getenv(ICON_PATH_ENV, "").strip()
    if not path:
        return None
    content_type = _ICON_TYPES.get(os.path.splitext(path)[1].lower())
    if content_type is None:
        return None
    try:
        mtime = os.stat(path).st_mtime
        cached = _icon_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        with open(path, "rb") as handle:
            data = handle.read(_ICON_MAX_BYTES + 1)
    except OSError:
        return None
    if not data or len(data) > _ICON_MAX_BYTES:
        return None
    _icon_cache[path] = (mtime, (data, content_type))
    return data, content_type
