"""
Startup guard for serving artifacts.

The viewer opens user-authored script inside the user's browser. An instance that also
holds write scopes (Gmail, Docs, full Drive, ...) must not serve it, so the viewer refuses
to start unless the scopes this instance requests are only those the viewer needs.
"""

import os
from typing import Iterable, List

from auth.scopes import DRIVE_READONLY_SCOPE, OPENID_SCOPE
from auth.scopes import USERINFO_EMAIL_SCOPE, USERINFO_PROFILE_SCOPE

ALLOW_WRITE_ENV = "WORKSPACE_MCP_ARTIFACTS_ALLOW_WRITE_WITH_SERVING"

SERVING_SCOPES = frozenset(
    {USERINFO_EMAIL_SCOPE, USERINFO_PROFILE_SCOPE, OPENID_SCOPE, DRIVE_READONLY_SCOPE}
)


def excess_scopes(scopes: Iterable[str]) -> List[str]:
    """Requested scopes beyond what the viewer needs."""
    return sorted(set(scopes) - SERVING_SCOPES)


def serving_scope_problem(scopes: Iterable[str]) -> str:
    """Why this instance must not serve artifacts, or an empty string when it may."""
    if os.getenv(ALLOW_WRITE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return ""
    extra = excess_scopes(scopes)
    if not extra:
        return ""
    return (
        "The artifact viewer is enabled on an instance that requests more than "
        f"Drive read access ({len(extra)} extra scopes, for example {extra[0]}). "
        "Run it as its own endpoint with only the artifacts tools, or set "
        f"{ALLOW_WRITE_ENV}=true to accept the risk."
    )
