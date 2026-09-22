"""Short claim-check links for signed download URLs (this fork).

A signed download URL from :mod:`core.signed_downloads` is self-contained: the
Fernet token in ``/attachments/signed/{token}`` carries the resource, its owner
and its expiry, so the route needs no storage. It is also long — a Gmail
``attachmentId`` alone runs ~300 characters, so the link passes through the
model as several hundred tokens every time it is repeated.

This module adds the claim-check form on top, without changing what is served:
the full encrypted token is stored server-side under a random 128-bit handle,
and the link handed to the model is ``{base}/dl/{handle}`` (~22-char handle).
The ``/dl/{handle}`` route (see :mod:`core.server`) looks the token up and
hands it to ``signed_downloads.serve`` — the same checks, headers and status
codes as the long form; it never redirects to the long URL, which would expose
the token.

The handle *is* the capability: 128 bits from a CSPRNG is as unguessable as
the token's own authentication tag. The store's TTL is the token's remaining
life, a read-time ``exp`` backstop fails closed if a backend's TTL slips, and
``serve`` re-verifies the token's own expiry after that. Unlike the token, a
handle can be revoked by deleting the row.

Backed by the shared ``WORKSPACE_MCP_OAUTH_PROXY_*`` backend (Postgres or
Valkey, via :func:`core.storage.get_configured_kv_store`) when configured,
with records Fernet-encrypted under a handle-specific context, exactly like
:mod:`core.auth_handles`; otherwise an in-process store, which works
single-container. Whenever the handle cannot be stored, the caller keeps the
long link — a link is never dead because the store was.

On by default; ``WORKSPACE_MCP_SHORT_SIGNED_URLS=false`` keeps the long form
(e.g. multi-replica deployments deliberately run without a shared backend).
"""

import logging
import os
import re
import secrets
import time
from typing import Optional

logger = logging.getLogger(__name__)

FLAG_ENV = "WORKSPACE_MCP_SHORT_SIGNED_URLS"

# Distinct from the pre-rebuild ``signed_download_refs`` collection, whose
# records were bare claims: a record of either shape is simply "unknown" to
# the other image, never a half-parsed one.
_COLLECTION = "signed_download_tokens"

# 16 bytes → 22-char urlsafe handle; the whole security margin of the URL.
_HANDLE_BYTES = 16

# token_urlsafe output is [A-Za-z0-9_-]; bound the length so arbitrary path
# garbage never reaches the store as a key.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# The long form's path, as ``signed_downloads.mint_url`` builds it.
_LONG_PATH = "/attachments/signed/"

# Module-level singleton, same pattern as auth_handles.
_store = None
_store_built = False


def short_links_enabled() -> bool:
    """Opt-out flag: short links are the default on this fork."""
    return os.getenv(FLAG_ENV, "true").strip().lower() == "true"


def _build_store():
    """Build the key-value store once (encrypted shared backend if configured, else memory)."""
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
                fernet=Fernet(
                    key=derive_shared_fernet_key("workspace-download-handles")
                ),
            )
            logger.info(
                "Download handles: using encrypted shared %s store (%s)",
                configured.backend,
                configured.detail,
            )
        else:
            from key_value.aio.stores.memory import MemoryStore

            _store = MemoryStore()
            logger.info(
                "Download handles: no shared backend configured, using in-process "
                "store (single-instance only)."
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Download handle store unavailable: %s", exc)
        _store = None

    return _store


async def store_download_ref(token: str, ttl_seconds: float) -> Optional[str]:
    """Store a signed download token under a fresh random handle.

    Returns the handle, or None when the token is empty, the TTL is not
    positive, no store is available, or the write fails — the caller then keeps
    the long link.
    """
    if not token or ttl_seconds <= 0:
        return None
    store = _build_store()
    if store is None:
        return None
    handle = secrets.token_urlsafe(_HANDLE_BYTES)
    record = {"token": token, "exp": time.time() + ttl_seconds}
    try:
        await store.put(handle, record, collection=_COLLECTION, ttl=ttl_seconds)
        return handle
    except Exception as exc:
        logger.warning("Failed to store download handle: %s", exc)
        return None


async def load_download_ref(handle: str) -> Optional[str]:
    """Return the token for a handle, or None (unknown, expired, or malformed).

    Fails closed: a malformed handle, a missing record, a slipped TTL, or a
    record without a string token all yield None.
    """
    if not handle or not _HANDLE_RE.fullmatch(handle):
        return None
    store = _build_store()
    if store is None:
        return None
    try:
        record = await store.get(handle, collection=_COLLECTION)
    except Exception as exc:
        logger.warning("Failed to load download handle: %s", exc)
        return None
    if not record:
        return None
    # The store's TTL is the primary expiry; exp is a read-time backstop against
    # a backend whose TTL semantics slip. serve() re-checks the token's own exp.
    exp = record.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    token = record.get("token")
    if not isinstance(token, str) or not token:
        return None
    return token


def split_signed_url(url: str) -> Optional[tuple[str, str]]:
    """``(base, token)`` for a long-form signed URL, else None."""
    if not isinstance(url, str):
        return None
    base, sep, token = url.partition(_LONG_PATH)
    if not sep or not token or "/" in token:
        return None
    return base, token


async def shorten_signed_url(
    offer: Optional[tuple[str, int]],
) -> Optional[tuple[str, int]]:
    """Tool-side hook: turn ``signed_downloads.offer_url``'s ``(url, ttl)`` into
    the ``/dl/{handle}`` form when short links are on.

    Returns the offer unchanged when it is None, short links are off, the URL is
    not the long form this module knows, or the handle could not be stored —
    so the caller always has a link that works.
    """
    if not offer or not short_links_enabled():
        return offer
    url, ttl = offer
    parts = split_signed_url(url)
    if parts is None:
        return offer
    base, token = parts
    handle = await store_download_ref(token, ttl)
    if handle is None:
        logger.info("Short download link unavailable; returning the long link.")
        return offer
    return f"{base}/dl/{handle}", ttl
