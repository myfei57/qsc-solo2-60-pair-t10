"""Contract, error-code, audit and idempotency behaviour for the console."""

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

    def call(self, method: str, path: str, payload=None):
        response = self.server.dispatch(method, path, payload)
        body = json.loads(response.body.decode("utf-8"))
        return response.status, body, response.request_id

    def test_missing_type_and_range_have_distinct_codes_and_fields(self) -> None:
        status, body, request_id = self.call("POST", "/chlor/target", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "104003")
        self.assertEqual(body["details"]["issues"][0]["reason"], "missing_field")
        self.assertEqual(body["details"]["issues"][0]["field"], "demand")
        self.assertEqual(body["request_id"], request_id)

        status, body, _ = self.call("POST", "/chlor/target", {"demand": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "104004")
        self.assertEqual(body["details"]["issues"][0]["reason"], "invalid_type")

        status, body, _ = self.call("POST", "/chlor/target", {"demand": 2000})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "104005")
        self.assertEqual(body["details"]["issues"][0]["reason"], "value_out_of_range")

    def test_standard_endpoint_can_declare_default_while_critical_endpoint_rejects(self) -> None:
        status, body, _ = self.call("POST", "/simulate", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["ticks"]), 5)

        status, body, _ = self.call("POST", "/flow/replace", {"serial": "m1"})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "103003")

    def test_rejection_is_written_to_request_audit_with_matching_code(self) -> None:
        status, body, request_id = self.call("POST", "/chlor/target", {"demand": "bad"})
        self.assertEqual(status, 400)
        status, audit, _ = self.call("GET", "/request-audit")
        self.assertEqual(status, 200)
        entry = next(item for item in audit["entries"] if item["request_id"] == request_id)
        self.assertEqual(entry["status"], 400)
        self.assertEqual(entry["code"], body["code"])
        self.assertEqual(entry["outcome"], "rejected")
        self.assertEqual(entry["issues"][0]["field"], "demand")

    def test_idempotent_retry_replays_response_without_a_second_dose(self) -> None:
        payload = {"idempotency_key": "dose-1"}
        first_status, first, first_id = self.call("POST", "/chlor/dose", payload)
        self.assertEqual(first_status, 200)
        self.assertEqual(first["dose"], 0.5)

        second_status, second, second_id = self.call("POST", "/chlor/dose", payload)
        self.assertEqual(second_status, 200)
        self.assertEqual(second, first)
        self.assertEqual(second_id, first_id)

        _, audit, _ = self.call("GET", "/audit")
        self.assertEqual(len(audit["entries"]), 1)

    def test_same_idempotency_key_with_different_payload_is_rejected(self) -> None:
        self.call("POST", "/chlor/target", {"idempotency_key": "k1", "demand": 0.5})
        status, body, _ = self.call(
            "POST", "/chlor/target", {"idempotency_key": "k1", "demand": 0.7}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "117011")

    def test_partial_cycle_failure_can_resume_without_repeating_completed_steps(self) -> None:
        self.call("POST", "/filter/add", {"id": "b1", "zone": 1, "load": 3.0})
        payload = {"flow": 800.0, "level": 10.0, "bed_id": "b1", "amount": 12.0}
        original = self.server.runtime.accumulator.add
        calls = {"quota": 0, "coag": 0}

        def failing_quota(_amount):
            calls["quota"] += 1
            if calls["quota"] == 1:
                raise ValueError("quota journal unavailable")
            return original(_amount)

        def counting_coag(_flow, operation_key=""):
            calls["coag"] += 1
            return self.server.runtime.coag_doser.__class__.update_flow_and_dose(
                self.server.runtime.coag_doser, _flow, operation_key
            )

        self.server.runtime.accumulator.add = failing_quota
        self.server.runtime.coag_doser.update_flow_and_dose = counting_coag
        status, body, _ = self.call("POST", "/cycle", payload)
        self.assertEqual(status, 202)
        self.assertEqual(body["code"], "109010")
        self.assertIn("quota journal unavailable", body["error"])

        self.server.runtime.accumulator.add = original
        status, body, _ = self.call("POST", "/cycle", payload)
        self.assertEqual(status, 200)
        self.assertEqual(calls["coag"], 1)
        self.assertEqual(calls["quota"], 1)
        self.assertIn("quota", body["completed_steps"])

    def test_catalog_publishes_fixed_module_policy(self) -> None:
        status, body, _ = self.call("GET", "/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(body["error_code_policy"], "global registry with fixed module segments")
        target = next(
            item
            for item in body["contracts"]
            if item["method"] == "POST" and item["path"] == "/chlor/target"
        )
        self.assertEqual(target["criticality"], "critical")
        self.assertEqual(target["missing_field_policy"], "reject_request")


if __name__ == "__main__":
    unittest.main()
