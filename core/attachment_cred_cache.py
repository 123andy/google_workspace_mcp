"""Short-TTL encrypted credential cache for signed attachment streaming (Design A).

The signed ``/attachments/signed`` route needs the attachment owner's Google
credentials, but the GET carries no bearer token and — behind multiple replicas —
may land on a different process than the tool call that minted the URL. In-memory
session state (``auth.oauth21_session_store``) is per-process, so it is not a
reliable source there.

This module gives the route a *shared*, short-lived credential lookup keyed by
email. When the tool mints a signed URL (inside the authenticated request, where
the credentials are in hand) it stashes a minimal credential record here; the
route reads it back. Records are Fernet-encrypted with the same key derivation as
the OAuth proxy's Valkey storage, and expire on the same horizon as the URL, so a
credential record never outlives the link it backs.

Backed by the shared ``WORKSPACE_MCP_OAUTH_PROXY_*`` storage backend (Valkey
or Postgres — see ``core.storage``) when one is configured, as in the
stateless / hosted deployment and the local PoC stack. Falls back to an
in-process store otherwise, which still works locally because the single
container serves both the tool and the route.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

_COLLECTION = "attachment_cred_cache"

# Module-level singleton. The tool and route run in the same uvicorn event loop,
# so a single lazily-built store is reused across requests.
_store = None
_store_built = False


def _derive_storage_key() -> bytes:
    """Derive a Fernet key, mirroring the OAuth proxy's storage encryption.

    Same inputs (JWT signing key override → else client secret) but a *distinct*
    salt, so this cache is a separate cryptographic context from the proxy's
    client storage even though both live in the same Valkey.
    """
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key

    override = os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", "").strip()
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()

    if override:
        jwt_key = derive_jwt_key(
            low_entropy_material=override, salt="fastmcp-jwt-signing-key"
        )
    elif client_secret:
        jwt_key = derive_jwt_key(
            high_entropy_material=client_secret, salt="fastmcp-jwt-signing-key"
        )
    else:
        raise ValueError(
            "Attachment credential cache requires GOOGLE_OAUTH_CLIENT_SECRET or "
            "FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY for encryption."
        )

    return derive_jwt_key(
        high_entropy_material=jwt_key.decode(),
        salt="workspace-attachment-cred-cache",
    )


def _build_store():
    """Build the encrypted key-value store once (shared backend if configured, else memory)."""
    global _store, _store_built
    if _store_built:
        return _store
    _store_built = True

    try:
        from cryptography.fernet import Fernet
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

        from core.storage import get_configured_kv_store

        # Same selection (and same store instance / connection pool) as the
        # OAuth proxy's client_storage, so cross-replica recovery works
        # whenever the proxy itself is backed by shared storage.
        configured = get_configured_kv_store()
        if configured is not None and configured.needs_encryption:
            _store = FernetEncryptionWrapper(
                key_value=configured.store, fernet=Fernet(key=_derive_storage_key())
            )
            logger.info(
                "Attachment credential cache: using encrypted shared %s store (%s)",
                configured.backend,
                configured.detail,
            )
        else:
            from key_value.aio.stores.memory import MemoryStore

            _store = MemoryStore()
            logger.info(
                "Attachment credential cache: no shared backend configured, using "
                "in-process store (single-instance only)."
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Attachment credential cache unavailable: %s", exc)
        _store = None

    return _store


def _credentials_to_record(credentials: Credentials) -> dict:
    expiry = credentials.expiry
    expiry_iso = None
    if expiry is not None:
        # Credentials.expiry is naive UTC; serialize as ISO for transport.
        expiry_iso = expiry.replace(tzinfo=timezone.utc).isoformat()
    return {
        "access_token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or []),
        "expiry": expiry_iso,
    }


def _record_to_credentials(record: dict) -> Credentials:
    expiry = None
    if record.get("expiry"):
        parsed = datetime.fromisoformat(record["expiry"])
        # google-auth expects a naive UTC datetime.
        expiry = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return Credentials(
        token=record.get("access_token"),
        refresh_token=record.get("refresh_token"),
        token_uri=record.get("token_uri"),
        client_id=record.get("client_id"),
        client_secret=record.get("client_secret"),
        scopes=record.get("scopes") or [],
        expiry=expiry,
    )


async def stash_credentials(
    user_email: str, credentials: Credentials, ttl_seconds: float
) -> bool:
    """Cache the owner's credentials for the lifetime of a signed URL.

    Returns True if stored, False if no store is available. Failures are
    swallowed (best-effort): the same-process in-memory session path still covers
    the local case, so a cache miss degrades rather than breaks.
    """
    store = _build_store()
    if store is None:
        return False
    try:
        await store.put(
            user_email,
            _credentials_to_record(credentials),
            collection=_COLLECTION,
            ttl=ttl_seconds,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Failed to cache attachment credentials for %s: %s", user_email, exc
        )
        return False


async def load_credentials(user_email: str) -> Optional[Credentials]:
    """Recover cached credentials by email, or None if absent/expired/unavailable."""
    store = _build_store()
    if store is None:
        return None
    try:
        record = await store.get(user_email, collection=_COLLECTION)
    except Exception as exc:
        logger.warning("Failed to read cached credentials for %s: %s", user_email, exc)
        return None
    if not record:
        return None
    try:
        return _record_to_credentials(record)
    except Exception as exc:
        logger.warning("Malformed cached credential record for %s: %s", user_email, exc)
        return None
