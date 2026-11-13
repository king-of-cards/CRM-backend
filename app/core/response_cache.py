"""Tiny in-memory response cache with "stale-while-revalidate".

get(key, build, fresh=60, stale=3600):
  - younger than `fresh` seconds  -> returned straight away
  - younger than `stale` seconds  -> returned straight away, and rebuilt in the background
  - missing or older               -> built now (callers for the same key wait for one build)
So after the first load, every request is answered from memory, while the data keeps
updating behind the scenes. warm() fills a key ahead of time (e.g. at server start).
"""
import logging
import threading
import time
from collections.abc import Callable, Hashable
from typing import Any

logger = logging.getLogger(__name__)

_entries: dict[Hashable, tuple[float, Any]] = {}
_locks: dict[Hashable, threading.Lock] = {}
_refreshing: set[Hashable] = set()
_guard = threading.Lock()
MAX_ENTRIES = 500


def _lock_for(key: Hashable) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _store(key: Hashable, value: Any) -> None:
    with _guard:
        if len(_entries) >= MAX_ENTRIES and key not in _entries:
            oldest = min(_entries, key=lambda k: _entries[k][0])
            _entries.pop(oldest, None)
        _entries[key] = (time.monotonic(), value)


def _refresh_in_background(key: Hashable, build: Callable[[], Any]) -> None:
    with _guard:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run() -> None:
        try:
            _store(key, build())
        except Exception:  # noqa: BLE001  (keep serving the old value)
            logger.warning("Background refresh of %r failed", key, exc_info=True)
        finally:
            with _guard:
                _refreshing.discard(key)

    threading.Thread(target=run, daemon=True, name=f"cache-refresh-{key!r}"[:60]).start()


def get(key: Hashable, build: Callable[[], Any], *, fresh: float = 60, stale: float = 3600) -> Any:
    now = time.monotonic()
    hit = _entries.get(key)
    if hit:
        age = now - hit[0]
        if age < fresh:
            return hit[1]
        if age < stale:
            _refresh_in_background(key, build)
            return hit[1]

    with _lock_for(key):  # one build per key; others wait and reuse it
        hit = _entries.get(key)
        if hit and time.monotonic() - hit[0] < fresh:
            return hit[1]
        value = build()
        _store(key, value)
        return value


def warm(key: Hashable, build: Callable[[], Any]) -> None:
    """Build now in the background, e.g. at startup or right after a sync."""
    _refresh_in_background(key, build)


def clear(prefix: str | None = None) -> None:
    """Forget cached values (all, or keys that are tuples starting with `prefix`)."""
    with _guard:
        for key in list(_entries):
            if prefix is None or (isinstance(key, tuple) and key and key[0] == prefix):
                _entries.pop(key, None)