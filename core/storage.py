"""Shared helpers for creating the key-value stores used across the server.

Two things live here:

- ``make_sanitized_file_store`` — disk-backed store factory shared by the
  OAuth-proxy server storage and the CLI token storage.
- ``get_configured_kv_store`` — the single reader of the
  ``WORKSPACE_MCP_OAUTH_PROXY_*`` storage env vars. Every consumer of the
  shared backend (the FastMCP OAuth proxy's ``client_storage`` in
  ``core.server``, and any other cross-replica cache) builds its store
  through this helper, so backend selection cannot drift between callers.
  Callers apply their own encryption wrappers (distinct Fernet keys/salts)
  on top of the shared base store.

Backends: ``valkey``, ``postgres``, ``disk``, ``memory``. Selection is by
explicit ``WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND``; with no explicit
backend, a set ``..._VALKEY_HOST`` implies valkey (legacy behavior), then a
set ``..._POSTGRES_DSN`` implies postgres.
"""

import logging
import os
import string
from dataclasses import dataclass
from typing import Any, Optional

from key_value.aio._utils.sanitization import HybridSanitizationStrategy
from key_value.aio.stores.filetree import FileTreeStore

logger = logging.getLogger(__name__)

SAFE_FILENAME_CHARS = string.ascii_letters + string.digits + "-_."
"""Characters allowed in on-disk file names for key-value stores."""

DEFAULT_POSTGRES_TABLE = "workspace_mcp_kv"
DEFAULT_POSTGRES_SWEEP_INTERVAL_SECONDS = 900.0


def make_sanitized_file_store(data_directory: str) -> FileTreeStore:
    """Return a ``FileTreeStore`` using the project-wide sanitization rules.

    Both the OAuth-proxy server storage and the CLI token storage need
    identical sanitization; this factory keeps them in sync.
    """
    return FileTreeStore(
        data_directory=data_directory,
        key_sanitization_strategy=HybridSanitizationStrategy(
            allowed_characters=SAFE_FILENAME_CHARS,
        ),
    )


@dataclass(frozen=True)
class ConfiguredKvStore:
    """A backend store selected from env config, plus caller-facing metadata."""

    store: Any
    backend: str  # "valkey" | "postgres" | "disk" | "memory"
    detail: str  # human-readable config summary; must never contain secrets
    needs_encryption: bool  # False only for the in-process memory backend


# Built once per process so every caller shares one connection pool / client.
_configured: Optional[ConfiguredKvStore] = None
_configured_built = False


def get_configured_kv_store() -> Optional[ConfiguredKvStore]:
    """Return the shared env-configured KV store, building it on first call.

    Returns None when no backend is configured, or when the configured
    backend cannot be built (a warning is logged); callers fall back to
    their own defaults in that case.
    """
    global _configured, _configured_built
    if not _configured_built:
        _configured = build_configured_kv_store()
        _configured_built = True
    return _configured


def build_configured_kv_store() -> Optional[ConfiguredKvStore]:
    """Uncached backend selection; prefer ``get_configured_kv_store``."""
    backend = os.getenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "").strip().lower()
    valkey_host = os.getenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", "").strip()
    postgres_dsn = os.getenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_DSN", "").strip()

    if backend == "valkey" or (not backend and valkey_host):
        return _build_valkey_store(valkey_host)
    if backend == "postgres" or (not backend and postgres_dsn):
        return _build_postgres_store(postgres_dsn)
    if backend == "disk":
        return _build_disk_store()
    if backend == "memory":
        from key_value.aio.stores.memory import MemoryStore

        return ConfiguredKvStore(
            store=MemoryStore(),
            backend="memory",
            detail="in-process MemoryStore",
            needs_encryption=False,
        )
    if backend:
        logger.warning(
            "Unknown WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND %r; using default storage.",
            backend,
        )
    return None


def derive_shared_fernet_key(salt: str) -> bytes:
    """Derive a Fernet key for encrypting records in the shared KV store.

    Mirrors the OAuth proxy's storage-encryption derivation (JWT signing key
    override → else Google client secret) so every consumer keys off the same
    deployment secret, but with a caller-chosen salt so each consumer is a
    distinct cryptographic context inside the same store.
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
            "Encrypted shared storage requires GOOGLE_OAUTH_CLIENT_SECRET or "
            "FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY."
        )

    return derive_jwt_key(high_entropy_material=jwt_key.decode(), salt=salt)


def _build_valkey_store(valkey_host: str) -> Optional[ConfiguredKvStore]:
    # Upstream's builder reads every WORKSPACE_MCP_OAUTH_PROXY_VALKEY_* setting
    # (host included) and its store reconnects after a dropped connection.
    del valkey_host
    try:
        from core.valkey_storage import (
            ResilientValkeyStore,
            build_valkey_client_config,
            configure_glide_logging,
            describe_valkey_client_config,
        )
    except ImportError as exc:
        logger.warning(
            "Valkey KV storage requested but Valkey dependencies are not installed (%s). "
            "Install 'workspace-mcp[valkey]' (or 'py-key-value-aio[valkey]', which includes 'valkey-glide') "
            "or unset WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND/WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST.",
            exc,
        )
        return None
    try:
        valkey_config = build_valkey_client_config()
    except ValueError as exc:
        logger.warning(
            "Invalid Valkey configuration; falling back to default storage (%s).",
            exc,
        )
        return None
    configure_glide_logging()
    return ConfiguredKvStore(
        store=ResilientValkeyStore(config=valkey_config),
        backend="valkey",
        detail=describe_valkey_client_config(valkey_config),
        needs_encryption=True,
    )


def _build_postgres_store(postgres_dsn: str) -> Optional[ConfiguredKvStore]:
    try:
        from core.storage_postgres import SweepingPostgreSQLStore
    except ImportError as exc:
        logger.warning(
            "Postgres KV storage requested but Postgres dependencies are not installed (%s). "
            "Install 'workspace-mcp[postgres]' (or 'py-key-value-aio[postgresql]', which includes 'asyncpg') "
            "or unset WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND/WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_DSN.",
            exc,
        )
        return None
    try:
        table_name = (
            os.getenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_TABLE", "").strip()
            or DEFAULT_POSTGRES_TABLE
        )
        sweep_raw = os.getenv(
            "WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_SWEEP_INTERVAL_SECONDS", ""
        ).strip()
        sweep_interval = (
            float(sweep_raw) if sweep_raw else DEFAULT_POSTGRES_SWEEP_INTERVAL_SECONDS
        )
        pool_min = int(
            os.getenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_POOL_MIN", "1").strip()
        )
        pool_max = int(
            os.getenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_POOL_MAX", "5").strip()
        )

        # The DSN carries credentials; pass it through but never log it.
        url_kwargs = {"url": postgres_dsn} if postgres_dsn else {}
        store = SweepingPostgreSQLStore(
            table_name=table_name,
            sweep_interval_seconds=sweep_interval,
            pool_min_size=pool_min,
            pool_max_size=pool_max,
            **url_kwargs,
        )
    except ValueError as exc:
        logger.warning(
            "Invalid Postgres KV storage configuration; falling back to default storage (%s).",
            exc,
        )
        return None
    detail = (
        f"table={table_name}, sweep_interval={sweep_interval:g}s, "
        f"pool={pool_min}-{pool_max}"
    )
    if not postgres_dsn:
        detail += ", host=localhost (no DSN configured)"
    return ConfiguredKvStore(
        store=store, backend="postgres", detail=detail, needs_encryption=True
    )


def _build_disk_store() -> Optional[ConfiguredKvStore]:
    try:
        disk_directory = os.getenv(
            "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", ""
        ).strip()
        if not disk_directory:
            # Default to FASTMCP_HOME/oauth-proxy or ~/.fastmcp/oauth-proxy
            fastmcp_home = os.getenv("FASTMCP_HOME", "").strip()
            if fastmcp_home:
                disk_directory = os.path.join(fastmcp_home, "oauth-proxy")
            else:
                disk_directory = os.path.expanduser("~/.fastmcp/oauth-proxy")

        store = make_sanitized_file_store(disk_directory)
        return ConfiguredKvStore(
            store=store,
            backend="disk",
            detail=f"directory={disk_directory}",
            needs_encryption=True,
        )
    except ImportError as exc:
        logger.warning(
            "Disk storage requested but dependencies not available (%s). "
            "Falling back to default storage.",
            exc,
        )
        return None
