"""Contract, idempotency, audit correlation and rollback guarantees."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.store import Store


class ContractCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload=None, headers=None):
        response = self.server.dispatch(method, path, payload, headers)
        return response.status, json.loads(response.body.decode("utf-8")), response

    def test_missing_type_and_range_have_distinct_codes(self) -> None:
        cases = (
            ({}, "COMMON-0005", "flow"),
            ({"flow": "abc"}, "COMMON-0007", "flow"),
            ({"flow": -1}, "COMMON-0008", "flow"),
        )
        for payload, expected_code, field_name in cases:
            with self.subTest(payload=payload):
                status, body, response = self.call("POST", "/coag/dose", payload)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], expected_code)
                self.assertEqual(body["error"]["stage"], "validate")
                self.assertEqual(body["error"]["details"][0]["field"], field_name)
                self.assertEqual(response.extra_headers[0][0], "X-Request-Id")
                self.assertEqual(
                    response.extra_headers[0][1],
                    body["error"]["request_id"],
                )

    def test_rejection_is_durable_and_queryable_by_request_id(self) -> None:
        status, body, _ = self.call("POST", "/coag/dose", {"flow": "bad"})
        self.assertEqual(status, 400)
        request_id = body["error"]["request_id"]

        status, journal, _ = self.call("GET", "/requests", headers=None)
        self.assertEqual(status, 200)
        matches = [
            item for item in journal["requests"] if item["request_id"] == request_id
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["code"], "COMMON-0007")
        self.assertEqual(matches[0]["stage"], "validate")

    def test_completed_idempotent_request_replays_without_rerunning_business(self) -> None:
        headers = {"Idempotency-Key": "dose-once"}
        status, first, _ = self.call("POST", "/coag/dose", {"flow": 5.0}, headers)
        self.assertEqual(status, 200)
        status, second, response = self.call("POST", "/coag/dose", {"flow": 5.0}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(second, first)
        self.assertTrue(any(name == "Idempotent-Replay" for name, _ in response.extra_headers))

        status, conflict, _ = self.call("POST", "/coag/dose", {"flow": 6.0}, headers)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "COMMON-0302")

    def test_partial_failure_rolls_back_and_same_key_can_retry(self) -> None:
        def fail() -> float:
            raise RuntimeError("actuator offline")

        headers = {"Idempotency-Key": "partial"}
        self.server.runtime.coag_doser.dose_from_persisted_flow = fail
        status, body, _ = self.call("POST", "/coag/dose", {"flow": 99.0}, headers)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "COMMON-4001")
        self.assertTrue(body["error"]["retryable"])

        _, flow_before, _ = self.call("GET", "/intake/flow")
        self.assertEqual(flow_before["flow"], 1.0)
        _, audit_before, _ = self.call("GET", "/audit")
        self.assertEqual(audit_before["entries"], [])

        del self.server.runtime.coag_doser.dose_from_persisted_flow
        status, retry, _ = self.call("POST", "/coag/dose", {"flow": 99.0}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(retry["dose"], 99.0)

    def test_successful_business_audit_matches_request_id(self) -> None:
        headers = {
            "X-Request-Id": "request-correlation-1",
            "Idempotency-Key": "correlation-dose",
        }
        status, _, _ = self.call("POST", "/coag/dose", {"flow": 5.0}, headers)
        self.assertEqual(status, 200)
        _, audit, _ = self.call("GET", "/audit")
        self.assertEqual(audit["entries"][0]["request_id"], "request-correlation-1")


if __name__ == "__main__":
    unittest.main()
