"""Tests for KV-store backend selection and the Postgres expiry sweeper.

Backend selection (``core.storage.build_configured_kv_store``) is pure
construction — none of the stores connect until first use — so these tests
run without any external services. A real-Postgres round-trip runs only when
``TEST_POSTGRES_DSN`` is set (mirroring the credential-store tests).
"""

import asyncio
import os

import pytest

from core import storage

asyncpg = pytest.importorskip("asyncpg", reason="postgres extra not installed")

from core.storage_postgres import SweepingPostgreSQLStore  # noqa: E402
from key_value.aio.stores.postgresql import PostgreSQLStore  # noqa: E402

_ENV_VARS = [
    "WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND",
    "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST",
    "WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_DSN",
    "WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_TABLE",
    "WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_SWEEP_INTERVAL_SECONDS",
    "WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_POOL_MIN",
    "WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_POOL_MAX",
    "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Reset the process-wide singleton so each test builds fresh.
    monkeypatch.setattr(storage, "_configured", None)
    monkeypatch.setattr(storage, "_configured_built", False)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


class TestBackendSelection:
    def test_no_config_returns_none(self):
        assert storage.build_configured_kv_store() is None

    def test_unknown_backend_returns_none(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "etcd")
        assert storage.build_configured_kv_store() is None

    def test_memory_backend(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "memory")
        configured = storage.build_configured_kv_store()
        assert configured is not None
        assert configured.backend == "memory"
        assert configured.needs_encryption is False

    def test_explicit_postgres_backend(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "postgres")
        configured = storage.build_configured_kv_store()
        assert configured is not None
        assert configured.backend == "postgres"
        assert configured.needs_encryption is True
        assert isinstance(configured.store, SweepingPostgreSQLStore)
        assert configured.store._table_name == storage.DEFAULT_POSTGRES_TABLE

    def test_dsn_implies_postgres(self, monkeypatch):
        monkeypatch.setenv(
            "WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_DSN",
            "postgresql://user:secret@db.example.com:5432/mcp",
        )
        configured = storage.build_configured_kv_store()
        assert configured is not None
        assert configured.backend == "postgres"
        # The DSN carries a password and must never leak into loggable detail.
        assert "secret" not in configured.detail
        assert "db.example.com" not in configured.detail

    def test_explicit_backend_beats_other_env(self, monkeypatch):
        """An explicit backend choice wins even if another backend's env is set."""
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", "valkey.internal")
        configured = storage.build_configured_kv_store()
        assert configured is not None
        assert configured.backend == "postgres"

    def test_postgres_env_overrides(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_TABLE", "custom_kv")
        monkeypatch.setenv(
            "WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_SWEEP_INTERVAL_SECONDS", "60"
        )
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_POOL_MIN", "2")
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_POOL_MAX", "8")
        configured = storage.build_configured_kv_store()
        store = configured.store
        assert store._table_name == "custom_kv"
        assert store._sweep_interval_seconds == 60.0
        assert store._pool_min_size == 2
        assert store._pool_max_size == 8

    def test_invalid_table_name_falls_back(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_TABLE", "1bad; DROP")
        assert storage.build_configured_kv_store() is None

    def test_invalid_pool_bounds_fall_back(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_POOL_MIN", "5")
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_POOL_MAX", "2")
        assert storage.build_configured_kv_store() is None

    def test_disk_backend(self, monkeypatch, tmp_path):
        from key_value.aio.stores.filetree import FileTreeStore

        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "disk")
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(tmp_path))
        configured = storage.build_configured_kv_store()
        assert configured is not None
        assert configured.backend == "disk"
        assert configured.needs_encryption is True
        assert isinstance(configured.store, FileTreeStore)

    def test_get_configured_kv_store_is_cached(self, monkeypatch):
        """Both consumers must share one instance (one pool per process)."""
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "memory")
        first = storage.get_configured_kv_store()
        second = storage.get_configured_kv_store()
        assert first is second
        assert first.store is second.store

    def test_get_configured_kv_store_caches_none(self, monkeypatch):
        assert storage.get_configured_kv_store() is None
        # A later env change must not alter the already-built process state.
        monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "memory")
        assert storage.get_configured_kv_store() is None


# ---------------------------------------------------------------------------
# Sweep scheduling (no database needed)
# ---------------------------------------------------------------------------


def _make_store(**kwargs) -> SweepingPostgreSQLStore:
    kwargs.setdefault("url", "postgresql://user@localhost/testdb")
    return SweepingPostgreSQLStore(**kwargs)


class _FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now


@pytest.fixture()
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr("core.storage_postgres.time", clock)
    return clock


@pytest.fixture()
def sweep_recorder(monkeypatch):
    """Replace _sweep_expired with a recorder so no DB is touched."""
    calls = []

    async def _record(self):
        calls.append(1)

    monkeypatch.setattr(SweepingPostgreSQLStore, "_sweep_expired", _record)
    return calls


@pytest.mark.asyncio
class TestSweepScheduling:
    async def test_first_operation_sweeps_immediately(self, fake_clock, sweep_recorder):
        store = _make_store()
        store._maybe_schedule_sweep()
        await asyncio.sleep(0)
        assert len(sweep_recorder) == 1

    async def test_within_interval_does_not_resweep(self, fake_clock, sweep_recorder):
        store = _make_store(sweep_interval_seconds=900)
        store._maybe_schedule_sweep()
        await asyncio.sleep(0)
        fake_clock.now += 899
        store._maybe_schedule_sweep()
        await asyncio.sleep(0)
        assert len(sweep_recorder) == 1

    async def test_after_interval_sweeps_again(self, fake_clock, sweep_recorder):
        store = _make_store(sweep_interval_seconds=900)
        store._maybe_schedule_sweep()
        await asyncio.sleep(0)
        fake_clock.now += 901
        store._maybe_schedule_sweep()
        await asyncio.sleep(0)
        assert len(sweep_recorder) == 2

    async def test_zero_interval_disables_sweeping(self, fake_clock, sweep_recorder):
        store = _make_store(sweep_interval_seconds=0)
        store._maybe_schedule_sweep()
        await asyncio.sleep(0)
        assert len(sweep_recorder) == 0

    async def test_pending_sweep_is_not_duplicated(self, fake_clock, monkeypatch):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def _slow_sweep(self):
            calls.append(1)
            started.set()
            await release.wait()

        monkeypatch.setattr(SweepingPostgreSQLStore, "_sweep_expired", _slow_sweep)
        store = _make_store(sweep_interval_seconds=900)
        store._maybe_schedule_sweep()
        await started.wait()
        # Interval elapsed but the previous sweep is still running.
        fake_clock.now += 5000
        store._maybe_schedule_sweep()
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        await store._sweep_task

    async def test_get_and_put_trigger_scheduling(
        self, fake_clock, sweep_recorder, monkeypatch
    ):
        async def _fake_get(self, key, *, collection=None):
            return None

        async def _fake_put(self, key, value, *, collection=None, ttl=None):
            return None

        monkeypatch.setattr(PostgreSQLStore, "get", _fake_get)
        monkeypatch.setattr(PostgreSQLStore, "put", _fake_put)

        store = _make_store(sweep_interval_seconds=900)
        assert await store.get("k") is None
        await asyncio.sleep(0)
        assert len(sweep_recorder) == 1

        fake_clock.now += 901
        await store.put("k", {"v": 1}, ttl=60)
        await asyncio.sleep(0)
        assert len(sweep_recorder) == 2


@pytest.mark.asyncio
class TestSweepExecution:
    """The DELETE itself, against a fake pool."""

    class _FakePool:
        def __init__(self, fail: bool = False):
            self.fail = fail
            self.queries = []

        async def execute(self, query, *args):
            self.queries.append(query)
            if self.fail:
                raise RuntimeError("connection lost")
            return "DELETE 3"

    async def test_sweep_deletes_only_expired_rows(self):
        store = _make_store(table_name="my_kv")
        pool = self._FakePool()
        store._pool = pool
        await store._sweep_expired()
        assert pool.queries == [
            "DELETE FROM my_kv WHERE expires_at IS NOT NULL AND expires_at < now()"
        ]

    async def test_sweep_failure_is_swallowed(self):
        store = _make_store()
        store._pool = self._FakePool(fail=True)
        await store._sweep_expired()  # must not raise


# ---------------------------------------------------------------------------
# Real-Postgres round-trip (opt-in)
# ---------------------------------------------------------------------------

TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.mark.integration
@pytest.mark.skipif(not TEST_DSN, reason="TEST_POSTGRES_DSN not set")
@pytest.mark.asyncio
class TestPostgresRoundTrip:
    async def test_put_get_ttl_and_sweep(self):
        store = SweepingPostgreSQLStore(
            url=TEST_DSN,
            table_name="test_workspace_mcp_kv",
            sweep_interval_seconds=0,  # manual sweeps only
        )
        async with store:
            await store.put("alive", {"v": 1}, collection="t", ttl=3600)
            await store.put("dead", {"v": 2}, collection="t", ttl=0.001)
            await asyncio.sleep(0.05)

            assert await store.get("alive", collection="t") == {"v": 1}
            # TTL is enforced on read even before any sweep runs.
            assert await store.get("dead", collection="t") is None

            # The expired row is still physically present until swept.
            count_sql = (
                "SELECT count(*) FROM test_workspace_mcp_kv WHERE collection = 't'"
            )
            assert await store._pool.fetchval(count_sql) == 2
            await store._sweep_expired()
            assert await store._pool.fetchval(count_sql) == 1

            await store._pool.execute("DROP TABLE test_workspace_mcp_kv")
