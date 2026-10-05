"""Persisted request audit journal.

This journal is separate from the chemical dosing audit: that audit answers
"what chemical action happened", while this one answers "why did the console
accept or reject an API request".  Both carry the same ``request_id``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

REQUEST_JOURNAL_KEY = "console:requests"


@dataclass(frozen=True)
class RequestRecord:
    """The terminal outcome of one HTTP request."""

    request_id: str
    idempotency_key: str
    method: str
    path: str
    status: int
    code: str
    stage: str
    message: str
    details: list[dict[str, object]]
    time: str

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "details": self.details,
            "time": self.time,
        }


class RequestJournal:
    """Append-only, durable record of request decisions."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record(
        self,
        *,
        request_id: str,
        idempotency_key: str,
        method: str,
        path: str,
        status: int,
        code: str,
        stage: str,
        message: str,
        details: list[dict[str, object]] | None = None,
    ) -> RequestRecord:
        entry = RequestRecord(
            request_id=request_id,
            idempotency_key=idempotency_key,
            method=method.upper(),
            path=path,
            status=status,
            code=code,
            stage=stage,
            message=message,
            details=details or [],
            time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        append_command(
            self._store,
            REQUEST_JOURNAL_KEY,
            json.dumps(entry.as_dict(), ensure_ascii=False, sort_keys=True),
        )
        return entry

    def entries(self) -> list[RequestRecord]:
        records: list[RequestRecord] = []
        for raw in load_commands(self._store, REQUEST_JOURNAL_KEY):
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            records.append(
                RequestRecord(
                    request_id=str(item.get("request_id", "")),
                    idempotency_key=str(item.get("idempotency_key", "")),
                    method=str(item.get("method", "")),
                    path=str(item.get("path", "")),
                    status=_as_int(item.get("status")),
                    code=str(item.get("code", "")),
                    stage=str(item.get("stage", "")),
                    message=str(item.get("message", "")),
                    details=item.get("details", []) if isinstance(item.get("details"), list) else [],
                    time=str(item.get("time", "")),
                )
            )
        return records


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
