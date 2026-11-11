"""The ``/ul/`` relay's cross-replica lease on a real Postgres (opt-in).

Set ``TEST_POSTGRES_DSN`` to run, e.g. against a throwaway container:
``docker run -d --rm -e POSTGRES_PASSWORD=x -p 55433:5432 postgres:16`` and
``TEST_POSTGRES_DSN=postgresql://postgres:x@localhost:55433/postgres``.
Each "replica" is its own store with its own pool, as in production.
"""

import asyncio
import os

import pytest
import pytest_asyncio
from fastapi.responses import Response

pytest.importorskip("asyncpg", reason="postgres extra not installed")

import core.upload_relay as ur  # noqa: E402
from core.storage_postgres import SweepingPostgreSQLStore  # noqa: E402

TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
_TABLE = "test_upload_relay_leases"
_C = ur._LEASE_COLLECTION

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DSN, reason="TEST_POSTGRES_DSN not set"),
]


@pytest_asyncio.fixture
async def replicas():
    stores = [
        SweepingPostgreSQLStore(
            url=TEST_DSN, table_name=_TABLE, pool_min_size=1, pool_max_size=2
        )
        for _ in range(2)
    ]
    # One at a time: two first-time CREATE TABLE IF NOT EXISTS can collide in
    # Postgres's catalog (the races below are about leases, not table setup).
    for store in stores:
        await store.setup()
    try:
        yield stores
    finally:
        await stores[0]._pool.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        for store in stores:
            await store.close()


async def _expire(store, key):
    await store._pool.execute(
        f"UPDATE {_TABLE} SET expires_at = now() - interval '1 second' "
        "WHERE collection = $1 AND key = $2",
        _C,
        key,
    )


async def _owner(store, key):
    return await store._pool.fetchval(
        f"SELECT value->>'owner' FROM {_TABLE} WHERE collection = $1 AND key = $2",
        _C,
        key,
    )


@pytest.mark.asyncio
async def test_one_holder_release_and_takeover(replicas):
    a, b = replicas
    assert await a.acquire_lease(_C, "h", "A", 90)
    assert not await b.acquire_lease(_C, "h", "B", 90)
    assert await _owner(a, "h") == "A"

    assert not await b.release_lease(_C, "h", "B")  # not B's to release
    assert await a.release_lease(_C, "h", "A")
    assert await b.acquire_lease(_C, "h", "B", 90)

    # B's replica dies: once its lease expires, A takes the link over.
    assert not await a.acquire_lease(_C, "h", "A2", 90)
    await _expire(a, "h")
    assert await a.acquire_lease(_C, "h", "A2", 90)
    # The stale owner can neither renew nor release the newer lease.
    assert not await b.renew_lease(_C, "h", "B", 90)
    assert not await b.release_lease(_C, "h", "B")
    assert await _owner(a, "h") == "A2"
    assert await a.renew_lease(_C, "h", "A2", 90)
    # Expired and not taken over: its old owner cannot renew it either.
    await _expire(a, "h")
    assert not await a.renew_lease(_C, "h", "A2", 90)


@pytest.mark.asyncio
async def test_renewal_moves_the_expiry_on_the_database_clock(replicas):
    a, _ = replicas
    assert await a.acquire_lease(_C, "r", "A", 5)
    left = await a._pool.fetchval(
        f"SELECT extract(epoch FROM expires_at - now()) FROM {_TABLE} "
        "WHERE collection = $1 AND key = $2",
        _C,
        "r",
    )
    assert 3 < float(left) <= 5
    assert await a.renew_lease(_C, "r", "A", 90)
    left = await a._pool.fetchval(
        f"SELECT extract(epoch FROM expires_at - now()) FROM {_TABLE} "
        "WHERE collection = $1 AND key = $2",
        _C,
        "r",
    )
    assert 85 < float(left) <= 90


@pytest.mark.asyncio
async def test_concurrent_acquires_have_exactly_one_winner(replicas):
    a, b = replicas
    for i in range(20):
        key = f"race-{i}"
        results = await asyncio.gather(
            a.acquire_lease(_C, key, "A", 90), b.acquire_lease(_C, key, "B", 90)
        )
        assert sorted(results) == [False, True], results
        # And again on an expired lease: two replicas racing for the takeover.
        await _expire(a, key)
        results = await asyncio.gather(
            a.acquire_lease(_C, key, "A2", 90), b.acquire_lease(_C, key, "B2", 90)
        )
        assert sorted(results) == [False, True], results


@pytest.mark.asyncio
async def test_two_replicas_serving_one_handle(replicas, monkeypatch):
    """The route's lock path on the real table: exactly one relay runs."""
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def held(handle, request):
        calls.append(handle)
        entered.set()
        await release.wait()
        return Response(status_code=200)

    monkeypatch.setattr(ur, "_serve_held", held)
    a, b = replicas
    first = asyncio.create_task(ur._serve_leased(a, "handle-1", None))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        second = await asyncio.wait_for(ur._serve_leased(b, "handle-1", None), 5)
    finally:
        release.set()
    assert second.status_code == 409
    release.set()
    assert (await first).status_code == 200
    assert calls == ["handle-1"]
    assert await _owner(a, "handle-1") is None  # released
    assert (await ur._serve_leased(b, "handle-1", None)).status_code == 200
