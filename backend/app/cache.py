"""A small thread-safe TTL cache for the in-process caches (Firecrawl, summaries,
extractions).

A plain dict of (expiry, value) only ever grows: an expired entry stays until the
same key is asked for again, which for a search box may be never. This one drops
expired entries as it goes and evicts the oldest past `max_entries`, so a
long-running server holds a bounded amount no matter how many distinct queries
it sees.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    def __init__(self, max_entries: int = 512) -> None:
        self._max = max_entries
        self._data: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> V | None:
        """The live value for `key`, or None when absent or expired."""
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            if hit[0] <= time.monotonic():
                del self._data[key]
                return None
            return hit[1]

    def set(self, key: str, value: V, ttl: float) -> None:
        now = time.monotonic()
        with self._lock:
            self._data[key] = (now + ttl, value)
            self._data.move_to_end(key)
            self._prune(now)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def _prune(self, now: float) -> None:
        """Drop expired entries, then the oldest ones past the size limit."""
        for key in [k for k, (expires, _) in self._data.items() if expires <= now]:
            del self._data[key]
        while len(self._data) > self._max:
            self._data.popitem(last=False)
