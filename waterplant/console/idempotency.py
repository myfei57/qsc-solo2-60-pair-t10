"""Durable idempotency records for mutating console operations."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

from waterplant.store.store import Store

IDEMPOTENCY_KEY_PREFIX = "console:idempotency:"
IN_PROGRESS_TTL_SECONDS = 300


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    method: str
    path: str
    fingerprint: str
    status: str
    response_status: int
    response_body: str
    created_at: float
    updated_at: float


class IdempotencyStore:
    """Persists terminal responses and short-lived in-progress markers."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._locks: dict[str, threading.Lock] = {}
        self._guards = threading.RLock()

    def lock_for(self, key: str) -> threading.Lock:
        with self._guards:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def load(self, key: str) -> IdempotencyRecord | None:
        raw, present = self._store.get(self._storage_key(key))
        if not present:
            return None
        try:
            item = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(item, dict):
            return None
        return IdempotencyRecord(
            key=str(item.get("key", key)),
            method=str(item.get("method", "")),
            path=str(item.get("path", "")),
            fingerprint=str(item.get("fingerprint", "")),
            status=str(item.get("status", "")),
            response_status=int(item.get("response_status", 0)),
            response_body=str(item.get("response_body", "")),
            created_at=float(item.get("created_at", 0.0)),
            updated_at=float(item.get("updated_at", 0.0)),
        )

    def begin(self, key: str, method: str, path: str, fingerprint: str) -> None:
        now = time.time()
        record = {
            "key": key,
            "method": method,
            "path": path,
            "fingerprint": fingerprint,
            "status": "in_progress",
            "response_status": 0,
            "response_body": "",
            "created_at": now,
            "updated_at": now,
        }
        self._put(key, record)

    def finish(self, key: str, method: str, path: str, fingerprint: str,
               status: int, body: str, created_at: float | None = None) -> None:
        now = time.time()
        record = {
            "key": key,
            "method": method,
            "path": path,
            "fingerprint": fingerprint,
            "status": "completed",
            "response_status": status,
            "response_body": body,
            "created_at": created_at if created_at is not None else now,
            "updated_at": now,
        }
        self._put(key, record)

    def stale_in_progress(self, record: IdempotencyRecord) -> bool:
        return record.status == "in_progress" and (
            time.time() - record.updated_at > IN_PROGRESS_TTL_SECONDS
        )

    def discard(self, key: str) -> None:
        """Remove a marker when the business unit rolled back and may be retried."""

        self._store.delete(self._storage_key(key))

    def _put(self, key: str, record: dict[str, object]) -> None:
        self._store.put(
            self._storage_key(key),
            json.dumps(record, ensure_ascii=False, sort_keys=True),
        )

    @staticmethod
    def _storage_key(key: str) -> str:
        return f"{IDEMPOTENCY_KEY_PREFIX}{key}"
