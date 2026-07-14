"""Short claim-check handles for Google authorization URLs.

A Google OAuth authorization URL is enormous — ~800 characters of scopes,
PKCE challenge, state, redirect_uri and login_hint — pages of query string
every time it passes through a chat model, and a long opaque string a model
can corrupt when it retypes it. ``91d3e9d`` softened the display by rendering
it as a markdown hyperlink; this module removes the length itself.

It is the direct analogue of :mod:`core.download_handles`: the full
authorization URL is stored server-side in the shared KV store, keyed by a
random 128-bit handle, and the link handed to the user carries only the
handle (~22 chars): ``{external_base}/auth/{handle}`` (~60 chars total). The
``/auth/{handle}`` route (see :mod:`core.server`) 302-redirects to the stored
URL.

The handle *is* the capability: 128 bits from a CSPRNG is unguessable, and the
store's TTL enforces the same 10-minute window the OAuth state already carries
(``store_oauth_state`` default). Records are Fernet-encrypted under a
handle-specific context, mirroring the download-handle store, so the full URL
(which embeds the PKCE challenge and state) is never at rest in the clear.

Backed by the shared ``WORKSPACE_MCP_OAUTH_PROXY_*`` backend (Valkey or
Postgres) when configured; falls back to an in-process store, which works
single-container. When no store is usable at all, ``store_auth_url_ref``
returns None and the caller falls back to the full authorization URL, which
always works.
"""

import logging
import re
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_COLLECTION = "auth_url_refs"

# 16 bytes → 22-char urlsafe handle; the whole security margin of the URL.
_HANDLE_BYTES = 16

# token_urlsafe output is [A-Za-z0-9_-]; bound the length so arbitrary path
# garbage never reaches the store as a key.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# The only host we ever mint authorization URLs for. Validated on load as
# defense-in-depth so /auth/{handle} can never become an open redirect, even if
# the store were somehow poisoned.
_ALLOWED_AUTH_HOST = "accounts.google.com"

# Module-level singleton, same pattern as download_handles / attachment_cred_cache.
_store = None
_store_built = False


def _build_store():
    """Build the encrypted key-value store once (shared backend if configured, else memory)."""
    global _store, _store_built
    if _store_built:
        return _store
    _store_built = True

    try:
        from cryptography.fernet import Fernet
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

        from core.storage import derive_shared_fernet_key, get_configured_kv_store

        configured = get_configured_kv_store()
        if configured is not None and configured.needs_encryption:
            _store = FernetEncryptionWrapper(
                key_value=configured.store,
                fernet=Fernet(key=derive_shared_fernet_key("workspace-auth-handles")),
            )
            logger.info(
                "Auth handles: using encrypted shared %s store (%s)",
                configured.backend,
                configured.detail,
            )
        else:
            from key_value.aio.stores.memory import MemoryStore

            _store = MemoryStore()
            logger.info(
                "Auth handles: no shared backend configured, using in-process "
                "store (single-instance only)."
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Auth handle store unavailable: %s", exc)
        _store = None

    return _store


def _is_allowed_auth_url(url: str) -> bool:
    """True only for the Google authorization endpoint we actually mint."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme == "https" and parsed.hostname == _ALLOWED_AUTH_HOST


async def store_auth_url_ref(auth_url: str, ttl_seconds: float) -> Optional[str]:
    """Store a full authorization URL under a fresh random handle.

    Returns the handle, or None when the URL is not a Google auth URL, no store
    is available, or the write fails — the caller then falls back to the full
    authorization URL.
    """
    if not _is_allowed_auth_url(auth_url):
        # Never store something we wouldn't be willing to redirect to.
        logger.warning("Refusing to store non-Google auth URL as a short handle.")
        return None
    store = _build_store()
    if store is None:
        return None
    handle = secrets.token_urlsafe(_HANDLE_BYTES)
    record = {"auth_url": auth_url, "exp": time.time() + ttl_seconds}
    try:
        await store.put(handle, record, collection=_COLLECTION, ttl=ttl_seconds)
        return handle
    except Exception as exc:
        logger.warning("Failed to store auth handle: %s", exc)
        return None


async def load_auth_url_ref(handle: str) -> Optional[str]:
    """Return the authorization URL for a handle, or None (unknown/expired/malformed).

    Fails closed: a malformed handle, missing record, slipped TTL, or a stored
    value that is not a Google authorization URL all yield None.
    """
    if not handle or not _HANDLE_RE.fullmatch(handle):
        return None
    store = _build_store()
    if store is None:
        return None
    try:
        record = await store.get(handle, collection=_COLLECTION)
    except Exception as exc:
        logger.warning("Failed to load auth handle: %s", exc)
        return None
    if not record:
        return None
    # The store's TTL is the primary expiry; the exp field is a read-time
    # backstop against a backend whose TTL semantics slip. Fail closed on a
    # missing or malformed exp.
    exp = record.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    auth_url = record.get("auth_url")
    if not isinstance(auth_url, str) or not _is_allowed_auth_url(auth_url):
        return None
    return auth_url
