"""OAuth state in a shared KV store: any replica can complete any consent."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

import auth.oauth_state_kv as oauth_state_kv
import core.storage as storage
from auth.oauth21_session_store import OAuth21SessionStore
from auth.oauth_state_kv import OAUTH_STATE_COLLECTION


def _shared_kv(inner=None):
    return FernetEncryptionWrapper(
        key_value=inner or MemoryStore(), fernet=Fernet(Fernet.generate_key())
    )


class _InterleavingKV:
    """Yields after every read so concurrent consumers all read before any delete."""

    def __init__(self, kv):
        self._kv = kv
        self.hits = 0

    async def put(self, key, value, *, collection=None, ttl=None):
        await self._kv.put(key, value, collection=collection, ttl=ttl)

    async def get(self, key, *, collection=None):
        value = await self._kv.get(key, collection=collection)
        if value is not None:
            self.hits += 1
        await asyncio.sleep(0)
        return value

    async def delete(self, key, *, collection=None):
        return await self._kv.delete(key, collection=collection)


class _FailingWriteKV:
    async def put(self, key, value, *, collection=None, ttl=None):
        raise ConnectionError("shared store down")

    async def get(self, key, *, collection=None):
        return None

    async def delete(self, key, *, collection=None):
        return False


def _replicas(tmp_path, kv):
    """Two store instances with separate local files and one shared KV."""
    return (
        OAuth21SessionStore(
            oauth_state_file=str(tmp_path / "a.json"), oauth_state_kv=kv
        ),
        OAuth21SessionStore(
            oauth_state_file=str(tmp_path / "b.json"), oauth_state_kv=kv
        ),
    )


@pytest.mark.asyncio
async def test_state_stored_on_one_replica_is_consumed_on_another(tmp_path):
    replica_a, replica_b = _replicas(tmp_path, _shared_kv())

    await replica_a.astore_oauth_state(
        "shared-state",
        session_id="session-123",
        code_verifier="verifier-123",
        expected_user_email="user@example.com",
        enforce_user_email_match=True,
        principal_source="gateway_assertion",
    )
    state_info = await replica_b.avalidate_and_consume_oauth_state(
        "shared-state", session_id="session-123"
    )

    assert state_info["session_id"] == "session-123"
    assert state_info["code_verifier"] == "verifier-123"
    assert state_info["expected_user_email"] == "user@example.com"
    assert state_info["enforce_user_email_match"] is True
    assert state_info["principal_source"] == "gateway_assertion"
    assert isinstance(state_info["expires_at"], datetime)
    # Nothing was written to either local file.
    assert not (tmp_path / "a.json").exists()
    assert not (tmp_path / "b.json").exists()


@pytest.mark.asyncio
async def test_shared_state_is_encrypted_at_rest(tmp_path):
    inner = MemoryStore()
    replica_a, _ = _replicas(tmp_path, _shared_kv(inner))

    await replica_a.astore_oauth_state("enc-state", code_verifier="secret-verifier")

    raw = await inner.get("enc-state", collection=OAUTH_STATE_COLLECTION)
    assert raw is not None
    assert "secret-verifier" not in repr(raw)


@pytest.mark.asyncio
async def test_shared_state_is_single_use(tmp_path):
    replica_a, replica_b = _replicas(tmp_path, _shared_kv())
    await replica_a.astore_oauth_state("once", code_verifier="v")

    await replica_b.avalidate_and_consume_oauth_state("once")
    with pytest.raises(ValueError, match="Invalid or expired"):
        await replica_a.avalidate_and_consume_oauth_state("once")


@pytest.mark.asyncio
async def test_concurrent_consumes_accept_exactly_one(tmp_path):
    kv = _InterleavingKV(_shared_kv())
    replica_a, replica_b = _replicas(tmp_path, kv)
    await replica_a.astore_oauth_state("raced", code_verifier="v")

    results = await asyncio.gather(
        replica_a.avalidate_and_consume_oauth_state("raced"),
        replica_b.avalidate_and_consume_oauth_state("raced"),
        return_exceptions=True,
    )

    # Both callbacks read the record before either deleted it ...
    assert kv.hits == 2
    # ... yet only the one whose delete removed it is accepted.
    accepted = [r for r in results if isinstance(r, dict)]
    rejected = [r for r in results if isinstance(r, ValueError)]
    assert len(accepted) == 1
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_expired_shared_state_is_rejected(tmp_path):
    """The stored expiry is checked on read, even if the backend TTL slipped."""
    kv = _shared_kv()
    _, replica_b = _replicas(tmp_path, kv)
    now = datetime.now(timezone.utc)
    await kv.put(
        "stale",
        {
            "session_id": None,
            "code_verifier": "v",
            "created_at": (now - timedelta(minutes=20)).isoformat(),
            "expires_at": (now - timedelta(minutes=10)).isoformat(),
        },
        collection=OAUTH_STATE_COLLECTION,
        ttl=600,
    )

    with pytest.raises(ValueError, match="Invalid or expired"):
        await replica_b.avalidate_and_consume_oauth_state("stale")


@pytest.mark.asyncio
async def test_shared_state_without_expiry_is_rejected(tmp_path):
    kv = _shared_kv()
    _, replica_b = _replicas(tmp_path, kv)
    await kv.put(
        "no-expiry",
        {"session_id": None, "code_verifier": "v"},
        collection=OAUTH_STATE_COLLECTION,
        ttl=600,
    )

    with pytest.raises(ValueError, match="Invalid or expired"):
        await replica_b.avalidate_and_consume_oauth_state("no-expiry")


@pytest.mark.asyncio
async def test_shared_state_session_mismatch_is_rejected(tmp_path):
    replica_a, replica_b = _replicas(tmp_path, _shared_kv())
    await replica_a.astore_oauth_state(
        "bound", session_id="session-1", code_verifier="v"
    )

    with pytest.raises(ValueError, match="does not match the initiating session"):
        await replica_b.avalidate_and_consume_oauth_state(
            "bound", session_id="session-2"
        )
    # As with the file store, a rejected state is consumed, not left for a retry.
    with pytest.raises(ValueError, match="Invalid or expired"):
        await replica_a.avalidate_and_consume_oauth_state(
            "bound", session_id="session-1"
        )


@pytest.mark.asyncio
async def test_unknown_state_is_rejected(tmp_path):
    _, replica_b = _replicas(tmp_path, _shared_kv())

    with pytest.raises(ValueError, match="Invalid or expired"):
        await replica_b.avalidate_and_consume_oauth_state("never-issued")


@pytest.mark.asyncio
async def test_without_shared_kv_the_state_file_is_used(tmp_path):
    state_file = tmp_path / "oauth_states.json"
    store_a = OAuth21SessionStore(oauth_state_file=str(state_file), oauth_state_kv=None)
    store_b = OAuth21SessionStore(oauth_state_file=str(state_file), oauth_state_kv=None)

    await store_a.astore_oauth_state(
        "file-state", session_id="session-1", code_verifier="v"
    )
    assert state_file.exists()

    state_info = await store_b.avalidate_and_consume_oauth_state(
        "file-state", session_id="session-1"
    )
    assert state_info["code_verifier"] == "v"
    with pytest.raises(ValueError, match="Invalid or expired"):
        await store_a.avalidate_and_consume_oauth_state("file-state")


@pytest.mark.asyncio
async def test_shared_write_failure_falls_back_to_state_file(tmp_path):
    state_file = tmp_path / "oauth_states.json"
    store = OAuth21SessionStore(
        oauth_state_file=str(state_file), oauth_state_kv=_FailingWriteKV()
    )

    await store.astore_oauth_state("fallback", session_id="s", code_verifier="v")

    assert state_file.exists()
    state_info = await store.avalidate_and_consume_oauth_state(
        "fallback", session_id="s"
    )
    assert state_info["code_verifier"] == "v"


class _CommitThenFailKV:
    """A put that commits and then reports failure (connection lost after the
    commit); optionally a delete that fails too."""

    def __init__(self, delete_fails=False):
        self.kv = MemoryStore()
        self.delete_fails = delete_fails

    async def put(self, key, value, *, collection=None, ttl=None):
        await self.kv.put(key, value, collection=collection, ttl=ttl)
        raise ConnectionError("connection lost after commit")

    async def get(self, key, *, collection=None):
        return await self.kv.get(key, collection=collection)

    async def delete(self, key, *, collection=None):
        if self.delete_fails:
            raise OSError("postgresql://u:hunter2@db/x")
        return await self.kv.delete(key, collection=collection)


@pytest.mark.asyncio
async def test_a_write_that_committed_before_failing_leaves_no_shared_copy(tmp_path):
    kv = _CommitThenFailKV()
    store = OAuth21SessionStore(
        oauth_state_file=str(tmp_path / "s.json"), oauth_state_kv=kv
    )
    await store.astore_oauth_state("committed", session_id="s", code_verifier="v")

    assert await kv.kv.get("committed", collection=OAUTH_STATE_COLLECTION) is None
    state_info = await store.avalidate_and_consume_oauth_state("committed", "s")
    assert state_info["code_verifier"] == "v"  # from the file, once
    with pytest.raises(ValueError):
        await store.avalidate_and_consume_oauth_state("committed", "s")


@pytest.mark.asyncio
async def test_a_failed_cleanup_still_falls_back_to_the_file(tmp_path, caplog):
    kv = _CommitThenFailKV(delete_fails=True)
    state_file = tmp_path / "s.json"
    store = OAuth21SessionStore(oauth_state_file=str(state_file), oauth_state_kv=kv)
    with caplog.at_level(logging.WARNING, logger="auth.oauth21_session_store"):
        await store.astore_oauth_state("blip", session_id="s", code_verifier="v")
    assert state_file.exists() and "blip" in state_file.read_text()
    assert "OSError" in caplog.text and "hunter2" not in caplog.text


@pytest.mark.asyncio
async def test_state_written_to_file_by_older_instance_is_still_accepted(tmp_path):
    """During a rollout, a callback may carry a state an old pod wrote to its file."""
    state_file = tmp_path / "oauth_states.json"
    old_pod = OAuth21SessionStore(oauth_state_file=str(state_file), oauth_state_kv=None)
    new_pod = OAuth21SessionStore(
        oauth_state_file=str(state_file), oauth_state_kv=_shared_kv()
    )

    old_pod.store_oauth_state("legacy", code_verifier="v")

    state_info = await new_pod.avalidate_and_consume_oauth_state("legacy")
    assert state_info["code_verifier"] == "v"


@pytest.mark.parametrize(
    "configured",
    [
        None,
        storage.ConfiguredKvStore(
            store=MemoryStore(),
            backend="memory",
            detail="in-process MemoryStore",
            needs_encryption=False,
        ),
    ],
)
def test_no_shared_store_without_a_shared_backend(monkeypatch, configured):
    monkeypatch.setattr(storage, "get_configured_kv_store", lambda: configured)

    assert oauth_state_kv.build_oauth_state_kv() is None


def test_shared_backend_builds_an_encrypted_store(monkeypatch):
    configured = storage.ConfiguredKvStore(
        store=MemoryStore(), backend="postgres", detail="test", needs_encryption=True
    )
    monkeypatch.setattr(storage, "get_configured_kv_store", lambda: configured)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")

    assert isinstance(oauth_state_kv.build_oauth_state_kv(), FernetEncryptionWrapper)


def test_shared_backend_without_a_key_falls_back_to_file(monkeypatch):
    configured = storage.ConfiguredKvStore(
        store=MemoryStore(), backend="postgres", detail="test", needs_encryption=True
    )
    monkeypatch.setattr(storage, "get_configured_kv_store", lambda: configured)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)

    assert oauth_state_kv.build_oauth_state_kv() is None


class _RecordingKV:
    def __init__(self, kv=None, *, put_error=None, get_error=None, delete_error=None):
        self._kv = kv or _shared_kv()
        self._put_error = put_error
        self._get_error = get_error
        self._delete_error = delete_error
        self.puts = 0

    async def put(self, key, value, *, collection=None, ttl=None):
        self.puts += 1
        if self._put_error:
            raise self._put_error
        await self._kv.put(key, value, collection=collection, ttl=ttl)

    async def get(self, key, *, collection=None):
        if self._get_error:
            raise self._get_error
        return await self._kv.get(key, collection=collection)

    async def delete(self, key, *, collection=None):
        if self._delete_error:
            raise self._delete_error
        return await self._kv.delete(key, collection=collection)


async def _store_in_shared(tmp_path, kv, state):
    writer = OAuth21SessionStore(
        oauth_state_file=str(tmp_path / "writer.json"), oauth_state_kv=kv
    )
    await writer.astore_oauth_state(state, code_verifier="v")


@pytest.mark.asyncio
async def test_delete_failure_rejects_the_state(tmp_path):
    """If single use cannot be confirmed, the callback fails closed."""
    shared = _shared_kv()
    await _store_in_shared(tmp_path, shared, "undeletable")
    reader = OAuth21SessionStore(
        oauth_state_file=str(tmp_path / "reader.json"),
        oauth_state_kv=_RecordingKV(shared, delete_error=ConnectionError("down")),
    )

    with pytest.raises(ValueError, match="Invalid or expired"):
        await reader.avalidate_and_consume_oauth_state("undeletable")


@pytest.mark.asyncio
async def test_read_failure_rejects_a_state_held_only_in_the_shared_store(tmp_path):
    shared = _shared_kv()
    await _store_in_shared(tmp_path, shared, "unreadable")
    reader = OAuth21SessionStore(
        oauth_state_file=str(tmp_path / "reader.json"),
        oauth_state_kv=_RecordingKV(shared, get_error=ConnectionError("down")),
    )

    with pytest.raises(ValueError, match="Invalid or expired"):
        await reader.avalidate_and_consume_oauth_state("unreadable")


@pytest.mark.asyncio
async def test_record_that_fails_to_decrypt_is_rejected(tmp_path):
    inner = MemoryStore()
    await _store_in_shared(tmp_path, _shared_kv(inner), "other-key")
    # Same backend, different Fernet key (e.g. a replica with another secret).
    reader = OAuth21SessionStore(
        oauth_state_file=str(tmp_path / "reader.json"),
        oauth_state_kv=_shared_kv(inner),
    )

    with pytest.raises(ValueError, match="Invalid or expired"):
        await reader.avalidate_and_consume_oauth_state("other-key")


@pytest.mark.asyncio
async def test_single_user_mode_keeps_states_in_the_file(tmp_path, monkeypatch):
    """The missing-state fallback only searches the file, so states must go there."""
    monkeypatch.setenv("MCP_SINGLE_USER_MODE", "1")
    kv = _RecordingKV()
    store = OAuth21SessionStore(
        oauth_state_file=str(tmp_path / "oauth_states.json"), oauth_state_kv=kv
    )

    await store.astore_oauth_state("stdio-state", code_verifier="stdio-verifier")

    assert kv.puts == 0
    state_info = store.consume_latest_oauth_state(allow_any_session=True)
    assert state_info is not None
    assert state_info["code_verifier"] == "stdio-verifier"


@pytest.mark.asyncio
async def test_already_expired_state_goes_to_the_file_as_before(tmp_path, caplog):
    kv = _RecordingKV()
    state_file = tmp_path / "oauth_states.json"
    store = OAuth21SessionStore(oauth_state_file=str(state_file), oauth_state_kv=kv)

    with caplog.at_level(logging.WARNING):
        await store.astore_oauth_state("zero", expires_in_seconds=0)

    assert kv.puts == 0
    assert "shared store" not in caplog.text
    with pytest.raises(ValueError, match="Invalid or expired"):
        await store.avalidate_and_consume_oauth_state("zero")


@pytest.mark.asyncio
async def test_shared_write_failure_warning_does_not_echo_the_error(tmp_path, caplog):
    dsn = "postgresql://user:hunter2@db.internal:5432/mcp"
    kv = _RecordingKV(put_error=OSError(f"could not connect to {dsn}"))
    store = OAuth21SessionStore(
        oauth_state_file=str(tmp_path / "oauth_states.json"), oauth_state_kv=kv
    )

    with caplog.at_level(logging.WARNING):
        await store.astore_oauth_state("leaky", code_verifier="v")

    assert "OSError" in caplog.text
    assert "hunter2" not in caplog.text


# ---------------------------------------------------------------------------
# Real Postgres (opt-in): set TEST_POSTGRES_DSN to run.
# ---------------------------------------------------------------------------

TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
_PG_TABLE = "test_oauth_state_kv"


@pytest.mark.integration
@pytest.mark.skipif(not TEST_DSN, reason="TEST_POSTGRES_DSN not set")
@pytest.mark.asyncio
async def test_real_postgres_cross_replica_single_use(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_DSN", TEST_DSN)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_TABLE", _PG_TABLE)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "dummy-client-secret")
    monkeypatch.delenv("MCP_SINGLE_USER_MODE", raising=False)

    # Two "pods": each builds its own pool the way production does.
    backends, replicas = [], []
    for name in ("a", "b"):
        configured = storage.build_configured_kv_store()
        assert configured is not None and configured.backend == "postgres"
        monkeypatch.setattr(storage, "get_configured_kv_store", lambda c=configured: c)
        kv = oauth_state_kv.build_oauth_state_kv()
        assert isinstance(kv, FernetEncryptionWrapper)
        backends.append(configured.store)
        replicas.append(
            OAuth21SessionStore(
                oauth_state_file=str(tmp_path / f"{name}.json"), oauth_state_kv=kv
            )
        )
    replica_a, replica_b = replicas

    async def rows(state):
        return await backends[0]._pool.fetchval(
            f"SELECT count(*) FROM {_PG_TABLE} WHERE collection = $1 AND key = $2",
            OAUTH_STATE_COLLECTION,
            state,
        )

    try:
        await replica_a.astore_oauth_state(
            "pg-state", session_id="s1", code_verifier="pg-verifier"
        )
        assert await rows("pg-state") == 1
        state_info = await replica_b.avalidate_and_consume_oauth_state(
            "pg-state", session_id="s1"
        )
        assert state_info["code_verifier"] == "pg-verifier"
        assert await rows("pg-state") == 0

        for attempt in range(20):
            state = f"pg-race-{attempt}"
            await replica_a.astore_oauth_state(state, code_verifier="v")
            results = await asyncio.gather(
                replica_a.avalidate_and_consume_oauth_state(state),
                replica_b.avalidate_and_consume_oauth_state(state),
                return_exceptions=True,
            )
            assert sum(isinstance(r, dict) for r in results) == 1, results
            assert sum(isinstance(r, ValueError) for r in results) == 1, results
            assert await rows(state) == 0
    finally:
        await backends[0]._pool.execute(f"DROP TABLE IF EXISTS {_PG_TABLE}")
        for backend in backends:
            await backend.close()
