"""Transaction boundary between validated input and business processing.

Validation completes before a :class:`UnitOfWork` begins. Business handlers run
against snapshots of durable state and the in-memory filter bank. If an
unexpected failure occurs after some steps have succeeded, every side effect in
the unit is restored before the response is returned. A retry therefore starts
from the pre-request state and the same Idempotency-Key never double-applies a
completed operation.
"""

from __future__ import annotations

from types import TracebackType

from waterplant.filter import Bank
from waterplant.store import Store


class UnitOfWork:
    """Snapshot/restore boundary for one request's business work."""

    def __init__(self, store: Store, bank: Bank | None = None) -> None:
        self._store = store
        self._bank = bank
        self._store_snapshot: dict[str, str] | None = None
        self._bank_snapshot: dict[str, object] | None = None
        self._active = False

    def begin(self) -> None:
        if self._active:
            return
        self._store_snapshot = self._store.snapshot()
        if self._bank is not None:
            self._bank_snapshot = self._bank.snapshot()
        self._active = True

    def commit(self) -> None:
        self._active = False

    def rollback(self, exc: BaseException | None = None) -> bool:
        if not self._active:
            return True
        recovered = self._restore()
        self._active = False
        return recovered

    def __enter__(self) -> "UnitOfWork":
        self.begin()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is None:
            self.commit()
            return False
        # Rollback removes business side effects. The server then chooses the
        # stable error code: deterministic ValueError remains a business
        # precondition, while unexpected exceptions are partial failures.
        self.rollback()
        return False

    def _restore(self) -> bool:
        try:
            if self._bank is not None and self._bank_snapshot is not None:
                self._bank.restore(self._bank_snapshot)  # type: ignore[arg-type]
            if self._store_snapshot is not None:
                self._store.restore(self._store_snapshot)
            return True
        except Exception:  # noqa: BLE001 - recovery failure must be reported as such
            return False
