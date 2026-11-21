import hashlib
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from entity import ContractError, base_schema, check, decide, load, record_ref, schema, shape


def fixtures():
    return {
        "bundle": load(ROOT / "examples/sample-bundle.json"),
        "host": load(ROOT / "examples/host-state.json"),
        "request": load(ROOT / "examples/read-request.json"),
    }


def replace_at(document, path, value):
    keys = [int(k) if k.isdigit() else k for k in path.split("/")]
    target = document
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value


def save_content(asset, value):
    asset["content"] = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    asset["sha256"] = hashlib.sha256(asset["content"].encode()).hexdigest()


class ReceiverTests(unittest.TestCase):
    def test_schema_is_valid(self):
        Draft202012Validator.check_schema(schema())

    def test_frozen_outcomes(self):
        for case in load(ROOT / "examples/frozen-cases.json"):
            with self.subTest(case=case["id"]):
                inputs = fixtures()
                for path, value in case["replace"].items():
                    replace_at(inputs, path, value)
                expected = case["expected"]
                if "contract_error" in expected:
                    with self.assertRaises(ContractError) as caught:
                        decide(**inputs)
                    self.assertEqual(caught.exception.code, expected["contract_error"])
                else:
                    self.assertEqual(decide(**inputs), expected)

    def test_extension_catches_what_the_base_schema_allows(self):
        bundle = fixtures()["bundle"]
        bundle["extensions"]["hyper_entity"]["claims"] = [{}]
        self.assertTrue(Draft202012Validator(base_schema()).is_valid(bundle))
        with self.assertRaises(ContractError) as caught:
            check(bundle)
        self.assertEqual(caught.exception.code, "INVALID_SAMPLEEXTENSION")

    def test_personal_role_uses_core_records_only(self):
        packet = load(ROOT / "interop/personal-record.json")
        shape(packet["entity"], "entity")
        shape(packet["claim"], "claim")
        self.assertEqual(packet["claim"]["subject"], record_ref(packet["entity"], "entity"))
        for row, kind in ((packet["entity"], "sampleEntity"), (packet["claim"], "sampleClaim")):
            with self.subTest(kind=kind), self.assertRaises(ContractError):
                shape(row, kind)

    def test_recompute_from_raw_record(self):
        for key, value in (("number", 1.44), ("unit", "kg/m³"), ("record_id", "another-row"),
                           ("method_version", "2"), ("temperature_c", 50)):
            with self.subTest(key=key):
                inputs = fixtures()
                raw = inputs["bundle"]["assets"][2]
                document = json.loads(raw["content"])
                document[key] = value
                save_content(raw, document)
                receipt = decide(**inputs)
                self.assertEqual(receipt["judgment"], "needs_review")
                self.assertIsNone(receipt["view"])

    def test_evidence_relations(self):
        for relation, expected in (("context", "needs_evidence"), ("contradicts", "needs_review")):
            with self.subTest(relation=relation):
                inputs = fixtures()
                bundle = inputs["bundle"]
                claim = bundle["extensions"]["hyper_entity"]["claims"][0]
                claim["evidence"][0]["relation"] = relation
                save_content(bundle["assets"][1], claim)
                receipt = decide(**inputs)
                self.assertEqual(receipt["judgment"], expected)
                self.assertIsNone(receipt["view"])

    def test_host_recheck_between_reads(self):
        inputs = fixtures()
        self.assertEqual(decide(**inputs)["execution"], "read")
        inputs["host"]["grants"][0]["status"] = "revoked"
        receipt = decide(**inputs)
        self.assertEqual(receipt["execution"], "blocked")
        self.assertIsNone(receipt["view"])

    def test_host_must_resolve_competing_current_grant_versions(self):
        inputs = fixtures()
        other = deepcopy(inputs["host"]["grants"][0])
        other["grant"]["version"] = "2"
        inputs["host"]["grants"].append(other)
        receipt = decide(**inputs)
        self.assertEqual(receipt["authorization"], "unknown")
        self.assertIsNone(receipt["view"])

    def test_missing_evidence_does_not_hide_conflict_by_list_order(self):
        inputs = fixtures()
        bundle = inputs["bundle"]
        raw = bundle["assets"][2]
        unavailable = {**raw, "asset_id": "previous-reading", "availability": "unavailable",
                       "content": None, "sha256": None}
        bundle["assets"].append(unavailable)
        claim = bundle["extensions"]["hyper_entity"]["claims"][0]
        link = deepcopy(claim["evidence"][0])
        link["asset_ref"]["asset_id"] = unavailable["asset_id"]
        claim["evidence"].append(link)
        document = json.loads(raw["content"])
        document["number"] = 1.33
        save_content(raw, document)
        for evidence in (claim["evidence"], list(reversed(claim["evidence"]))):
            claim["evidence"] = evidence
            save_content(bundle["assets"][1], claim)
            receipt = decide(**inputs)
            self.assertEqual(receipt["judgment"], "needs_review")
            self.assertIsNone(receipt["view"])


if __name__ == "__main__":
    unittest.main()
