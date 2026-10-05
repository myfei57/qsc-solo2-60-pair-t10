"""Durable idempotency records for retrying mutating console requests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from waterplant.store.store import Store

IDEMPOTENCY_KEY = "console:idempotency"


class IdempotencyConflict(Exception):
    """The same key was presented with a different request fingerprint."""


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    fingerprint: str
    status: int
    body: dict[str, object]
    request_id: str


class IdempotencyStore:
    def __init__(self, store: Store) -> None:
        self._store = store

    @staticmethod
    def fingerprint(method: str, path: str, payload: dict[str, object]) -> str:
        canonical = json.dumps(
            {"method": method.upper(), "path": path, "payload": _without_key(payload)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str, fingerprint: str) -> IdempotencyRecord | None:
        records = self._load()
        raw = records.get(key)
        if not isinstance(raw, dict):
            return None
        if raw.get("fingerprint") != fingerprint:
            raise IdempotencyConflict(key)
        body = raw.get("body", {})
        return IdempotencyRecord(
            key=key,
            fingerprint=fingerprint,
            status=int(raw.get("status", 200)),
            body=body if isinstance(body, dict) else {},
            request_id=str(raw.get("request_id", "")),
        )

    def save(
        self,
        key: str,
        fingerprint: str,
        status: int,
        body: dict[str, object],
        request_id: str,
    ) -> None:
        records = self._load()
        previous = records.get(key)
        if isinstance(previous, dict) and previous.get("fingerprint") != fingerprint:
            raise IdempotencyConflict(key)
        records[key] = {
            "fingerprint": fingerprint,
            "status": status,
            "body": body,
            "request_id": request_id,
        }
        self._store.put(IDEMPOTENCY_KEY, json.dumps(records, ensure_ascii=False, sort_keys=True))

    def _load(self) -> dict[str, object]:
        raw, present = self._store.get(IDEMPOTENCY_KEY)
        if not present:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


def _without_key(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "idempotency_key"}
