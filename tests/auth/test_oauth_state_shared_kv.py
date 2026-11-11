"""OAuth state in a shared KV store: any replica can complete any consent."""

import asyncio
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
