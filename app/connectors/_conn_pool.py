"""
Simple per-credentials connection pool for warehouse connectors.

Redshift and Snowflake both use sync DB-API connections run in a thread
pool. Opening a TCP+TLS connection to Redshift takes 0.5–2s — meaningful
overhead when users run many sequential queries. This module caches idle
connections per-credentials with a TTL and a max-size cap.

Design:
  - Key: hash of (host/account, user, database, warehouse/port) — never
    includes the password. Credentials are validated on checkout.
  - Keeps idle connections for IDLE_TTL seconds. Stale connections are
    closed on checkout.
  - Max MAX_PER_KEY connections per key, and MAX_TOTAL across all keys.
  - Thread-safe via an RLock (we're called from a ThreadPoolExecutor).

The pool is best-effort: on any error we fall back to building a fresh
connection. We never block the calling thread on a full pool — just
create a throw-away connection that's closed when done.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Pool configuration constants
IDLE_TTL = 180  # 3 min — drop connections idle longer than this
MAX_PER_KEY = 4  # max idle connections per credential set
MAX_TOTAL = 40  # global cap across all credential sets


class ConnectionPool:
    def __init__(
        self,
        idle_ttl: int = IDLE_TTL,
        max_per_key: int = MAX_PER_KEY,
        max_total: int = MAX_TOTAL,
    ):
        self._pools: dict[str, list[tuple[Any, float]]] = {}
        self._lock = threading.RLock()
        self._total = 0
        self.idle_ttl = idle_ttl
        self.max_per_key = max_per_key
        self.max_total = max_total

    @staticmethod
    def key_for(*parts: Any) -> str:
        """Generate a stable hash-based key from credential parts (host, user, db, etc.)."""
        raw = "|".join(str(p) for p in parts if p is not None)
        return hashlib.sha1(raw.encode()).hexdigest()

    def _take(self, key: str) -> Any | None:
        """Pop and return a valid idle connection from the pool, or None if empty/stale."""
        now = time.time()
        with self._lock:
            bucket = self._pools.get(key)
            if not bucket:
                return None
            # Pop from the end (LIFO reuses warm connections more)
            while bucket:
                conn, put_at = bucket.pop()
                self._total -= 1
                if now - put_at > self.idle_ttl:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                return conn
        return None

    def _put(self, key: str, conn: Any) -> bool:
        """Return a connection to the pool if under limits. Returns False if pool is full."""
        with self._lock:
            bucket = self._pools.setdefault(key, [])
            if len(bucket) >= self.max_per_key or self._total >= self.max_total:
                return False
            bucket.append((conn, time.time()))
            self._total += 1
            return True

    @contextmanager
    def checkout(self, key: str, builder: Callable[[], Any]):
        """
        Context manager: yield a connection from pool or build a fresh one.

        On normal exit: returns the connection to the pool if healthy.
        On exception: closes the connection (it may be in a bad state).
        If builder fails: yields None and caller should emit their own error.
        """
        conn = self._take(key)
        fresh = False
        if conn is None:
            conn = builder()
            fresh = True
        if conn is None:
            # Builder failed — propagate as a None-yield so the caller can
            # emit the engine-specific connection error.
            yield None
            return

        healthy = True
        try:
            yield conn
        except Exception:
            healthy = False
            raise
        finally:
            if not healthy:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                returned = self._put(key, conn)
                if not returned:
                    try:
                        conn.close()
                    except Exception:
                        pass
                elif fresh:
                    logger.debug(f"pool: new conn parked under key={key[:8]}")

    def snapshot(self) -> dict[str, Any]:
        """Return pool stats (for monitoring/debugging)."""
        with self._lock:
            return {
                "keys": len(self._pools),
                "idle_total": self._total,
                "per_key": {k[:8]: len(v) for k, v in self._pools.items()},
                "max_per_key": self.max_per_key,
                "max_total": self.max_total,
                "idle_ttl": self.idle_ttl,
            }

    def close_all(self) -> int:
        """Close all pooled connections and reset state (call on shutdown)."""
        closed = 0
        with self._lock:
            for bucket in self._pools.values():
                for conn, _ in bucket:
                    try:
                        conn.close()
                        closed += 1
                    except Exception:
                        pass
            self._pools.clear()
            self._total = 0
        return closed


# Shared pools — one per engine so their connection objects don't get mixed.
redshift_pool = ConnectionPool()
snowflake_pool = ConnectionPool()
