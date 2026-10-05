"""Resumable operation checkpoints for multi-step console requests.

A saga deliberately does not roll back completed physical actions. It records
each completed step so a retried operation can skip it, run the next
compensation/recovery action when one exists, and then continue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from waterplant.store.store import Store

SAGA_KEY = "console:sagas"


@dataclass(frozen=True)
class Step:
    name: str
    action: Callable[[], object]
    compensate: Callable[[], None] | None = None


class Saga:
    def __init__(self, store: Store, operation_key: str) -> None:
        self._store = store
        self._operation_key = operation_key
        self._records = self._load()
        record = self._records.setdefault(operation_key, {"completed": [], "results": {}})
        if not isinstance(record, dict):
            record = {"completed": [], "results": {}}
            self._records[operation_key] = record

    def run(self, step: Step) -> object:
        completed = self._completed()
        if step.name in completed:
            return self._results().get(step.name)
        try:
            result = step.action()
        except Exception:
            if step.compensate is not None:
                step.compensate()
            self._save()
            raise
        completed.append(step.name)
        self._results()[step.name] = result
        self._save()
        return result

    def completed_steps(self) -> list[str]:
        return list(self._completed())

    def _load(self) -> dict[str, object]:
        raw, present = self._store.get(SAGA_KEY)
        if not present:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _record(self) -> dict[str, object]:
        record = self._records[self._operation_key]
        assert isinstance(record, dict)
        return record

    def _completed(self) -> list[str]:
        completed = self._record().setdefault("completed", [])
        if not isinstance(completed, list):
            completed = []
            self._record()["completed"] = completed
        return completed

    def _results(self) -> dict[str, object]:
        results = self._record().setdefault("results", {})
        if not isinstance(results, dict):
            results = {}
            self._record()["results"] = results
        return results

    def _save(self) -> None:
        self._store.put(SAGA_KEY, json.dumps(self._records, ensure_ascii=False, sort_keys=True))
