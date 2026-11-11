"""Shared key-value store for pending OAuth ``state`` records.

Between ``start_auth_flow`` and the ``/oauth2callback`` redirect, the server
holds the OAuth state and its PKCE ``code_verifier``. By default those live in a
file on local disk (see ``OAuth21SessionStore``), which only works when the
callback reaches the same machine that started the flow, and survives a restart
only if that disk does.

When a shared backend is configured through ``WORKSPACE_MCP_OAUTH_PROXY_*``
(see :mod:`core.storage`), pending states are kept there instead. With Valkey
or Postgres, any replica can complete any consent and a restart during consent
does not lose it. The disk backend is shared only between processes on one
host (or on one shared volume). Records are Fernet-encrypted under their own key
context, because they carry the PKCE verifier.

The in-process ``memory`` backend is deliberately not used here: it is no more
shared than the state file, and the file at least survives a restart.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

OAUTH_STATE_COLLECTION = "oauth_states"
_FERNET_SALT = "workspace-oauth-states"

_store: Optional[Any] = None
_store_built = False


def build_oauth_state_kv() -> Optional[Any]:
    """Return an encrypted shared KV store for OAuth states, or None.

    None means "no shared backend": callers keep states in the local file.
    """
    try:
        from core.storage import get_configured_kv_store

        configured = get_configured_kv_store()
        if configured is None or not configured.needs_encryption:
            return None

        from cryptography.fernet import Fernet
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

        from core.storage import derive_shared_fernet_key

        store = FernetEncryptionWrapper(
            key_value=configured.store,
            fernet=Fernet(key=derive_shared_fernet_key(_FERNET_SALT)),
        )
        logger.info(
            "OAuth states: using encrypted shared %s store (%s)",
            configured.backend,
            configured.detail,
        )
        return store
    except Exception as exc:
        logger.warning(
            "OAuth states: shared store unavailable, using the local state file: %s",
            exc,
        )
        return None


def get_oauth_state_kv() -> Optional[Any]:
    """Process-wide shared OAuth state store, built on first use."""
    global _store, _store_built
    if not _store_built:
        _store = build_oauth_state_kv()
        _store_built = True
    return _store
