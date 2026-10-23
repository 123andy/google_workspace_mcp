"""
Registry of published artifact bundles.

A bundle is only served after its owner registers it; the registry maps the public
hash to the Drive folder. The default backend is SQLite from the standard library
(no new dependency); other backends implement ``ArtifactRegistry``.
"""

import logging
import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from gartifacts.artifacts_links import new_hash, slugify

logger = logging.getLogger(__name__)

REGISTRY_PATH_ENV = "WORKSPACE_MCP_ARTIFACTS_REGISTRY_PATH"
REGISTRY_BACKEND_ENV = "WORKSPACE_MCP_ARTIFACTS_REGISTRY_BACKEND"
_MAX_HASH_ATTEMPTS = 5


@dataclass(frozen=True)
class ArtifactRecord:
    """One registered bundle."""

    hash: str
    folder_id: str
    owner_email: str
    title: str
    slug: str
    registered_at: float
    last_seen: float


class ArtifactRegistry(ABC):
    """Storage for registered bundles."""

    @abstractmethod
    def register(self, folder_id: str, owner_email: str, title: str) -> ArtifactRecord:
        """Register a folder, or refresh an existing registration (idempotent)."""

    @abstractmethod
    def get_by_hash(self, artifact_hash: str) -> Optional[ArtifactRecord]:
        """Look up a bundle by its public hash."""

    @abstractmethod
    def get_by_folder(self, folder_id: str) -> Optional[ArtifactRecord]:
        """Look up a bundle by its Drive folder ID."""

    @abstractmethod
    def refresh(self, artifact_hash: str, title: str) -> Optional[ArtifactRecord]:
        """Update the title and slug and mark the bundle as seen."""


class SqliteArtifactRegistry(ArtifactRegistry):
    """SQLite registry. ``:memory:`` is supported for tests and stateless runs."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS artifacts (
            hash TEXT PRIMARY KEY,
            folder_id TEXT NOT NULL UNIQUE,
            owner_email TEXT NOT NULL,
            title TEXT NOT NULL,
            slug TEXT NOT NULL,
            registered_at REAL NOT NULL,
            last_seen REAL NOT NULL
        )
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._shared: Optional[sqlite3.Connection] = None
        if path == ":memory:":
            self._shared = self._connect()
        else:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with self._connection() as conn:
                conn.execute(self._SCHEMA)
            os.chmod(path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._path, timeout=10, isolation_level=None, check_same_thread=False
        )
        conn.execute(self._SCHEMA)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._shared or self._connect()
            try:
                yield conn
            finally:
                if self._shared is None:
                    conn.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            hash=row["hash"],
            folder_id=row["folder_id"],
            owner_email=row["owner_email"],
            title=row["title"],
            slug=row["slug"],
            registered_at=row["registered_at"],
            last_seen=row["last_seen"],
        )

    def register(self, folder_id: str, owner_email: str, title: str) -> ArtifactRecord:
        now = time.time()
        slug = slugify(title)
        owner = owner_email.strip().lower()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE folder_id = ?", (folder_id,)
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE artifacts SET owner_email = ?, title = ?, slug = ?, "
                        "last_seen = ? WHERE hash = ?",
                        (owner, title, slug, now, row["hash"]),
                    )
                    updated = conn.execute(
                        "SELECT * FROM artifacts WHERE hash = ?", (row["hash"],)
                    ).fetchone()
                    conn.execute("COMMIT")
                    return self._record(updated)
                for _ in range(_MAX_HASH_ATTEMPTS):
                    candidate = new_hash()
                    taken = conn.execute(
                        "SELECT 1 FROM artifacts WHERE hash = ?", (candidate,)
                    ).fetchone()
                    if taken is None:
                        conn.execute(
                            "INSERT INTO artifacts (hash, folder_id, owner_email, "
                            "title, slug, registered_at, last_seen) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (candidate, folder_id, owner, title, slug, now, now),
                        )
                        created = conn.execute(
                            "SELECT * FROM artifacts WHERE hash = ?", (candidate,)
                        ).fetchone()
                        conn.execute("COMMIT")
                        return self._record(created)
                conn.execute("ROLLBACK")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        raise RuntimeError("Could not allocate a unique artifact hash")

    def get_by_hash(self, artifact_hash: str) -> Optional[ArtifactRecord]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE hash = ?", (artifact_hash,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def get_by_folder(self, folder_id: str) -> Optional[ArtifactRecord]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE folder_id = ?", (folder_id,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def refresh(self, artifact_hash: str, title: str) -> Optional[ArtifactRecord]:
        now = time.time()
        with self._connection() as conn:
            conn.execute(
                "UPDATE artifacts SET title = ?, slug = ?, last_seen = ? WHERE hash = ?",
                (title, slugify(title), now, artifact_hash),
            )
            row = conn.execute(
                "SELECT * FROM artifacts WHERE hash = ?", (artifact_hash,)
            ).fetchone()
        return self._record(row) if row is not None else None


_registry: Optional[ArtifactRegistry] = None
_registry_lock = threading.Lock()


def _default_registry_path() -> str:
    explicit = os.getenv(REGISTRY_PATH_ENV, "").strip()
    if explicit:
        return explicit
    base = os.getenv("WORKSPACE_MCP_CREDENTIALS_DIR") or os.getenv(
        "GOOGLE_MCP_CREDENTIALS_DIR"
    )
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".google_workspace_mcp")
    return os.path.join(base, "artifacts.sqlite")


def _build_registry() -> ArtifactRegistry:
    backend = os.getenv(REGISTRY_BACKEND_ENV, "sqlite").strip().lower()
    if backend == "postgres":
        from gartifacts.artifacts_registry_postgres import PostgresArtifactRegistry

        return PostgresArtifactRegistry()
    if backend != "sqlite":
        raise ValueError(
            f"Unknown {REGISTRY_BACKEND_ENV} {backend!r}; use sqlite or postgres"
        )
    return SqliteArtifactRegistry(_default_registry_path())


def get_artifact_registry() -> ArtifactRegistry:
    """Return the process-wide registry, creating the SQLite default on first use."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = _build_registry()
        return _registry


def set_artifact_registry(registry: Optional[ArtifactRegistry]) -> None:
    """Install a registry (another backend, or a test double); None resets it."""
    global _registry
    with _registry_lock:
        _registry = registry
