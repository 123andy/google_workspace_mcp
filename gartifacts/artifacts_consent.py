"""
Browser consent for the artifact viewer.

A viewer who has not yet granted Drive read access is sent to Google's consent screen
by the viewer route; Google returns to the server's normal OAuth callback, which then
sends the browser back to the page they asked for. The return path is remembered per
OAuth state (in memory, like the OAuth state itself, so a restart makes the user start
the link again).
"""

import os
import threading
import time
from typing import Dict, Optional, Tuple
from urllib.parse import quote

from starlette.responses import HTMLResponse, RedirectResponse

from auth.google_auth import create_oauth_flow
from auth.oauth21_session_store import get_oauth21_session_store
from auth.scopes import DRIVE_READONLY_SCOPE, DRIVE_SCOPE, get_current_scopes
from core.config import get_oauth_redirect_uri

RETURN_TTL_SECONDS = 600
_MAX_PENDING = 2048

_pending: Dict[str, Tuple[str, str, float]] = {}
_lock = threading.Lock()


def is_safe_return_path(path: str) -> bool:
    """Only same-site viewer paths may be used as a return target."""
    return (
        path.startswith("/a/")
        and not path.startswith("//")
        and "\\" not in path
        and "\n" not in path
        and "\r" not in path
        and "://" not in path
    )


def remember_return(state: str, return_path: str, email: str) -> None:
    now = time.monotonic()
    with _lock:
        for key in [k for k, v in _pending.items() if v[2] <= now]:
            del _pending[key]
        if len(_pending) >= _MAX_PENDING:
            _pending.pop(next(iter(_pending)))
        _pending[state] = (return_path, email, now + RETURN_TTL_SECONDS)


def pop_return(state: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return (path, email) for a state once, or None if unknown or expired."""
    if not state:
        return None
    with _lock:
        entry = _pending.pop(state, None)
    if entry is None or entry[2] <= time.monotonic():
        return None
    return entry[0], entry[1]


def begin_consent(email: str, return_path: str) -> str:
    """Create the Google authorization URL for ``email`` and remember where to return."""
    if not is_safe_return_path(return_path):
        raise ValueError("Unsafe return path")
    state = os.urandom(16).hex()
    flow = create_oauth_flow(
        scopes=get_current_scopes(), redirect_uri=get_oauth_redirect_uri(), state=state
    )
    get_oauth21_session_store().store_oauth_state(
        state,
        session_id=None,
        code_verifier=flow.code_verifier,
        expected_user_email=email,
        enforce_user_email_match=True,
        principal_source="gateway_assertion",
    )
    remember_return(state, return_path, email)
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", login_hint=email
    )
    return auth_url


def drive_read_granted(credentials) -> bool:
    """True when Drive read access was actually granted (partial grants are possible)."""
    granted = set(getattr(credentials, "scopes", None) or [])
    return bool(granted & {DRIVE_READONLY_SCOPE, DRIVE_SCOPE})


def finish_viewer_consent(state: Optional[str], credentials):
    """After the OAuth callback: send the browser back, or explain what is missing.

    Returns None when ``state`` did not come from the viewer, so the caller falls back
    to its normal success page.
    """
    pending = pop_return(state)
    if pending is None:
        return None
    return_path, _email = pending
    if not drive_read_granted(credentials):
        return HTMLResponse(
            "<!doctype html><title>Access not granted</title>"
            "<p>Viewing artifacts needs permission to read your Google Drive files. "
            "Open the artifact link again and allow access.</p>",
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    return RedirectResponse(quote(return_path, safe="/%?=&-_.~"), status_code=302)
