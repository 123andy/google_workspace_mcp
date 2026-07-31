"""Short claim-check handles for signed download URLs.

A self-contained signed JWT URL is enormous — a Gmail ``attachmentId`` alone
runs ~300 characters, pushing the full URL past 600 characters (~250 LLM
tokens every time it passes through the model). This module implements the
claim-check alternative: the claims that would have been signed into the JWT
are stored server-side in the shared KV store, keyed by a random 128-bit
handle, and the URL carries only the handle (~22 chars): ``/dl/{handle}``.

The handle *is* the capability: 128 bits from a CSPRNG is as unguessable as
an HMAC signature, the store's TTL enforces the same expiry the JWT would
have carried, and — unlike a JWT — a handle can be revoked by deleting the
row. Records are Fernet-encrypted under a handle-specific context, mirroring
the attachment credential cache.

Backed by the shared ``WORKSPACE_MCP_OAUTH_PROXY_*`` backend (Valkey or
Postgres) when configured; falls back to an in-process store, which works
single-container. Multi-replica deployments need the shared backend — but
they already do, for credential recovery. When no store is usable at all,
``store_download_ref`` returns None and the caller falls back to the
self-contained JWT URL, which always works.
"""

import logging
import re
import secrets
import time
from typing import Optional

logger = logging.getLogger(__name__)

_COLLECTION = "signed_download_refs"

# 16 bytes → 22-char urlsafe handle; the whole security margin of the URL.
_HANDLE_BYTES = 16

# token_urlsafe output is [A-Za-z0-9_-]; bound the length so arbitrary path
# garbage never reaches the store as a key.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# Module-level singleton, same pattern as attachment_cred_cache.
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


async def store_download_ref(claims: dict, ttl_seconds: float) -> Optional[str]:
    """Store download claims under a fresh random handle.

    Returns the handle, or None when no store is available or the write fails —
    the caller then falls back to the self-contained JWT URL.
    """
    store = _build_store()
    if store is None:
        return None
    handle = secrets.token_urlsafe(_HANDLE_BYTES)
    try:
        await store.put(handle, dict(claims), collection=_COLLECTION, ttl=ttl_seconds)
        return handle
    except Exception as exc:
        logger.warning("Failed to store download handle: %s", exc)
        return None


async def load_download_ref(handle: str) -> Optional[dict]:
    """Return the claims for a handle, or None (unknown, expired, or malformed)."""
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
    # The store's TTL is the primary expiry; the exp claim (same value a signed
    # JWT would carry) is a backstop against a backend whose TTL semantics slip.
    # Fail closed: a record with a missing or malformed exp is rejected too.
    exp = record.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return record
