"""Postgres KV store with opportunistic deletion of expired rows.

``PostgreSQLStore`` enforces TTL only on read: expired rows are ignored by
``get`` but stay in the table until overwritten. Valkey, by contrast,
physically evicts expired keys. Since this table holds encrypted OAuth state
and short-lived credential stashes, expired ciphertext shouldn't accumulate —
so this subclass piggybacks a sweep on normal traffic: after a ``get``/
``put``, if more than ``sweep_interval_seconds`` have passed since the last
sweep in this process, a background task deletes expired rows.

Idle processes don't sweep, which is acceptable: no traffic writes no new
secrets, and the first request after an idle stretch (or a restart) cleans
up. Multiple replicas may sweep concurrently; the DELETE is idempotent, so
the race is harmless.

This module imports ``asyncpg`` at import time — import it lazily (see
``core.storage._build_postgres_store``) so the driver stays optional.

The store also offers leases (``acquire_lease`` / ``renew_lease`` /
``release_lease``): a row per key in the same table, held by an owner token
until ``expires_at``. The KV API has no put-if-absent, so these are single SQL
statements; each borrows a pool connection only for that statement, never for
the life of the lease. Times are the database's ``now()``, so replicas' clocks
do not matter. Expired lease rows are swept like any other.
"""

import asyncio
import json
import logging
import time
from typing import Any, Mapping, Optional, SupportsFloat

import asyncpg
from key_value.aio.stores.postgresql import PostgreSQLStore

logger = logging.getLogger(__name__)

DEFAULT_POOL_MIN_SIZE = 1
DEFAULT_POOL_MAX_SIZE = 5


class SweepingPostgreSQLStore(PostgreSQLStore):
    """``PostgreSQLStore`` that sweeps expired rows and bounds its pool size.

    The upstream store's pool defaults to min=max=10 connections; this table
    sees OAuth flows and signed-URL stashes, not request-rate traffic, so a
    couple of connections per process is plenty.
    """

    def __init__(
        self,
        *,
        sweep_interval_seconds: float = 900.0,
        pool_min_size: int = DEFAULT_POOL_MIN_SIZE,
        pool_max_size: int = DEFAULT_POOL_MAX_SIZE,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if pool_min_size < 1 or pool_max_size < pool_min_size:
            raise ValueError(
                f"Invalid pool bounds: min={pool_min_size}, max={pool_max_size}"
            )
        self._sweep_interval_seconds = sweep_interval_seconds
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        # None means "never swept in this process": the first operation after
        # boot sweeps immediately, clearing anything that expired while down.
        self._last_sweep_monotonic: Optional[float] = None
        self._sweep_task: Optional[asyncio.Task] = None

    async def _create_pool(self) -> asyncpg.Pool:
        if self._url:
            return await asyncpg.create_pool(
                self._url,
                min_size=self._pool_min_size,
                max_size=self._pool_max_size,
            )
        return await asyncpg.create_pool(
            host=self._host,
            port=self._port,
            database=self._database,
            user=self._user,
            password=self._password,
            min_size=self._pool_min_size,
            max_size=self._pool_max_size,
        )

    def _maybe_schedule_sweep(self) -> None:
        if self._sweep_interval_seconds <= 0:
            return
        now = time.monotonic()
        if (
            self._last_sweep_monotonic is not None
            and now - self._last_sweep_monotonic < self._sweep_interval_seconds
        ):
            return
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        # No await between the checks above and this assignment, so concurrent
        # coroutines on the event loop cannot double-schedule.
        self._last_sweep_monotonic = now
        self._sweep_task = asyncio.create_task(self._sweep_expired())

    async def _sweep_expired(self) -> None:
        if self._pool is None:  # not set up yet; nothing to sweep
            return
        try:
            result = await self._pool.execute(
                f"DELETE FROM {self._table_name} "  # noqa: S608 - table name validated in __init__
                "WHERE expires_at IS NOT NULL AND expires_at < now()"
            )
            logger.debug("Swept expired rows from %s: %s", self._table_name, result)
        except Exception as exc:
            # Best-effort: a failed sweep must never surface to the request
            # that happened to trigger it; the next interval retries.
            logger.warning("Expired-row sweep on %s failed: %s", self._table_name, exc)

    async def get(
        self, key: str, *, collection: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        value = await super().get(key, collection=collection)
        # Runs after super() so _setup() has completed and the pool exists.
        self._maybe_schedule_sweep()
        return value

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: Optional[str] = None,
        ttl: Optional[SupportsFloat] = None,
    ) -> None:
        await super().put(key, value, collection=collection, ttl=ttl)
        self._maybe_schedule_sweep()

    # --- Leases ---------------------------------------------------------------------

    async def acquire_lease(
        self, collection: str, key: str, owner: str, ttl_seconds: float
    ) -> bool:
        """Take the lease on ``key`` for ``owner`` if nobody holds it or the
        holder's lease has expired. True only for the caller that took it."""
        await self.setup()
        row = await self._initialized_pool.fetchval(
            f"INSERT INTO {self._table_name} AS t "  # noqa: S608 - table name validated in __init__
            "(collection, key, value, ttl, created_at, expires_at) "
            "VALUES ($1, $2, $3::jsonb, $4, now(), now() + make_interval(secs => $4)) "
            "ON CONFLICT (collection, key) DO UPDATE SET value = EXCLUDED.value, "
            "ttl = EXCLUDED.ttl, created_at = EXCLUDED.created_at, "
            "expires_at = EXCLUDED.expires_at "
            "WHERE t.expires_at IS NOT NULL AND t.expires_at <= now() "
            "RETURNING 1",
            collection,
            key,
            json.dumps({"owner": owner}),
            float(ttl_seconds),
        )
        return row == 1

    async def renew_lease(
        self, collection: str, key: str, owner: str, ttl_seconds: float
    ) -> bool:
        """Extend ``owner``'s lease to ``ttl_seconds`` from now. False when the
        row is gone, another owner has taken it, or it has expired: a holder
        paused past its expiry has lost the lease even if nobody took it."""
        await self.setup()
        row = await self._initialized_pool.fetchval(
            f"UPDATE {self._table_name} "  # noqa: S608 - table name validated in __init__
            "SET ttl = $4, expires_at = now() + make_interval(secs => $4) "
            "WHERE collection = $1 AND key = $2 AND value->>'owner' = $3 "
            "AND expires_at > now() "
            "RETURNING 1",
            collection,
            key,
            owner,
            float(ttl_seconds),
        )
        return row == 1

    async def release_lease(self, collection: str, key: str, owner: str) -> bool:
        """Drop the lease only if ``owner`` still holds it."""
        await self.setup()
        result = await self._initialized_pool.execute(
            f"DELETE FROM {self._table_name} "  # noqa: S608 - table name validated in __init__
            "WHERE collection = $1 AND key = $2 AND value->>'owner' = $3",
            collection,
            key,
            owner,
        )
        return result.split()[-1] != "0"
