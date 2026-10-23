"""
Switches for the optional artifacts feature.

Everything artifact-related is off unless explicitly enabled, so a server that sets none
of these variables behaves exactly as it did before the feature existed.
"""

import os
from typing import Optional
from urllib.parse import urlsplit

# Adds the artifacts tools to the default service set (they can also be selected
# explicitly with --tools artifacts or --permissions artifacts:readonly).
ARTIFACTS_ENV = "WORKSPACE_MCP_ARTIFACTS"
# Mounts the artifact viewer and content routes.
VIEWER_ENV = "WORKSPACE_MCP_ARTIFACTS_VIEWER"
# Public base URL of the viewer, used to build links returned to users.
VIEWER_URL_ENV = "WORKSPACE_MCP_ARTIFACTS_VIEWER_URL"

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE


def artifacts_enabled() -> bool:
    """True when the artifacts tools should load by default."""
    return _flag(ARTIFACTS_ENV)


def viewer_enabled() -> bool:
    """True when the viewer and content routes should be mounted."""
    return _flag(VIEWER_ENV)


def viewer_base_url() -> Optional[str]:
    """The viewer's public base URL without a trailing slash, or None if unset/invalid."""
    raw = os.getenv(VIEWER_URL_ENV, "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.path or parts.query or parts.fragment:
        return None
    return raw
