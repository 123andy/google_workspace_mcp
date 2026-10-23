"""
PostgreSQL backend for the artifact registry.

Durable and shared across replicas, unlike the SQLite default. Select it with
``WORKSPACE_MCP_ARTIFACTS_REGISTRY_BACKEND=postgres``; the DSN comes from
``WORKSPACE_MCP_ARTIFACTS_REGISTRY_POSTGRES_DSN`` or, if unset, from the credential
store's ``WORKSPACE_MCP_CREDENTIAL_POSTGRES_DSN`` so one database holds both.
"""

import logging
import os
import re
import threading
from typing import Any, Optional

from auth.credential_store import _default_pg_connect
from gartifacts.artifacts_links import new_hash, slugify
from gartifacts.artifacts_registry import ArtifactRecord, ArtifactRegistry

logger = logging.getLogger(__name__)

DSN_ENV = "WORKSPACE_MCP_ARTIFACTS_REGISTRY_POSTGRES_DSN"
TABLE_ENV = "WORKSPACE_MCP_ARTIFACTS_REGISTRY_POSTGRES_TABLE"
_MAX_HASH_ATTEMPTS = 5
_SCHEMA_LOCK_KEY = 7_426_017_112
_schema_lock = threading.Lock()
_COLUMNS = (
    "hash, folder_id, owner_email, title, slug, "
    "EXTRACT(EPOCH FROM registered_at), EXTRACT(EPOCH FROM last_seen)"
)


def _record(row: Any) -> ArtifactRecord:
    return ArtifactRecord(
        hash=row[0],
        folder_id=row[1],
        owner_email=row[2],
        title=row[3],
        slug=row[4],
        registered_at=float(row[5]),
        last_seen=float(row[6]),
    )


class PostgresArtifactRegistry(ArtifactRegistry):
    """Registry stored in a PostgreSQL table."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        table: Optional[str] = None,
        connect=_default_pg_connect,
    ):
        self.dsn = (
            dsn
            or os.getenv(DSN_ENV)
            or os.getenv("WORKSPACE_MCP_CREDENTIAL_POSTGRES_DSN")
        )
        if not self.dsn:
            raise ValueError(
                f"The Postgres artifact registry needs a DSN; set {DSN_ENV} "
                "or WORKSPACE_MCP_CREDENTIAL_POSTGRES_DSN."
            )
        self.table = table or os.getenv(TABLE_ENV, "artifacts")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.table):
            raise ValueError(f"Invalid artifact registry table name: {self.table!r}")
        self._connect = connect
        self._schema_ready = False

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with _schema_lock, self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                # Concurrent CREATE TABLE IF NOT EXISTS can still collide in the
                # catalog, so serialise it across processes with an advisory lock.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.table} (
                        hash TEXT PRIMARY KEY,
                        folder_id TEXT NOT NULL UNIQUE,
                        owner_email TEXT NOT NULL,
                        title TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
        self._schema_ready = True

    def register(self, folder_id: str, owner_email: str, title: str) -> ArtifactRecord:
        self._ensure_schema()
        slug = slugify(title)
        owner = owner_email.strip().lower()
        with self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                update = (
                    f"UPDATE {self.table} SET owner_email = %s, title = %s, "
                    f"slug = %s, last_seen = now() WHERE folder_id = %s "
                    f"RETURNING {_COLUMNS}"
                )
                cur.execute(update, (owner, title, slug, folder_id))
                row = cur.fetchone()
                if row is not None:
                    return _record(row)
                for _ in range(_MAX_HASH_ATTEMPTS):
                    cur.execute(
                        f"INSERT INTO {self.table} "
                        "(hash, folder_id, owner_email, title, slug) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING "
                        f"RETURNING {_COLUMNS}",
                        (new_hash(), folder_id, owner, title, slug),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        return _record(row)
                    # A conflict is either a hash collision (draw again) or a
                    # concurrent registration of this folder (update it).
                    cur.execute(update, (owner, title, slug, folder_id))
                    row = cur.fetchone()
                    if row is not None:
                        return _record(row)
        raise RuntimeError("Could not allocate a unique artifact hash")

    def _one(self, where: str, value: str) -> Optional[ArtifactRecord]:
        self._ensure_schema()
        with self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM {self.table} WHERE {where} = %s", (value,)
                )
                row = cur.fetchone()
        return _record(row) if row is not None else None

    def get_by_hash(self, artifact_hash: str) -> Optional[ArtifactRecord]:
        return self._one("hash", artifact_hash)

    def get_by_folder(self, folder_id: str) -> Optional[ArtifactRecord]:
        return self._one("folder_id", folder_id)

    def refresh(self, artifact_hash: str, title: str) -> Optional[ArtifactRecord]:
        self._ensure_schema()
        with self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self.table} SET title = %s, slug = %s, "
                    f"last_seen = now() WHERE hash = %s RETURNING {_COLUMNS}",
                    (title, slugify(title), artifact_hash),
                )
                row = cur.fetchone()
        return _record(row) if row is not None else None
