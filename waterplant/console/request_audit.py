"""Persisted audit trail for rejected and completed console requests."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

REQUEST_AUDIT_KEY = "console:request-audit"


@dataclass(frozen=True)
class RequestAuditEntry:
    request_id: str
    at: str
    method: str
    path: str
    status: int
    code: str
    outcome: str
    reason: str
    idempotency_key: str
    issues: tuple[dict[str, object], ...]
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "at": self.at,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "code": self.code,
            "outcome": self.outcome,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "issues": list(self.issues),
            "detail": self.detail,
        }


class RequestAuditor:
    """Writes one durable record per rejected or completed API request."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record(
        self,
        *,
        method: str,
        path: str,
        status: int,
        code: str,
        outcome: str,
        reason: str,
        request_id: str | None = None,
        idempotency_key: str = "",
        issues: list[dict[str, object]] | None = None,
        detail: str = "",
    ) -> RequestAuditEntry:
        entry = RequestAuditEntry(
            request_id=request_id or uuid.uuid4().hex,
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            method=method.upper(),
            path=path,
            status=status,
            code=code,
            outcome=outcome,
            reason=reason,
            idempotency_key=idempotency_key,
            issues=tuple(issues or []),
            detail=detail,
        )
        append_command(
            self._store, REQUEST_AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False)
        )
        return entry

    def entries(self) -> list[RequestAuditEntry]:
        result: list[RequestAuditEntry] = []
        for raw in load_commands(self._store, REQUEST_AUDIT_KEY):
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            issues = payload.get("issues", [])
            result.append(
                RequestAuditEntry(
                    request_id=str(payload.get("request_id", "")),
                    at=str(payload.get("at", "")),
                    method=str(payload.get("method", "")),
                    path=str(payload.get("path", "")),
                    status=int(payload.get("status", 0)),
                    code=str(payload.get("code", "")),
                    outcome=str(payload.get("outcome", "")),
                    reason=str(payload.get("reason", "")),
                    idempotency_key=str(payload.get("idempotency_key", "")),
                    issues=tuple(issues if isinstance(issues, list) else []),
                    detail=str(payload.get("detail", "")),
                )
            )
        return result

    def find(self, request_id: str) -> RequestAuditEntry | None:
        for entry in reversed(self.entries()):
            if entry.request_id == request_id:
                return entry
        return None
