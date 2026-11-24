"""Offline receiver for the synthetic inspection.sample.density/1 profile."""

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from upstream.tools.validate_bundle import validate_bundle


ROOT = Path(__file__).resolve().parents[1]
PROFILE = {"id": "inspection.sample.density", "version": "1"}
PURPOSE = "sample_result_review"
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=ValueError)
def is_timestamp(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)", value):
        return False
    return instant(value).tzinfo is not None


class ContractError(ValueError):
    def __init__(self, code, path, detail):
        super().__init__(detail)
        self.code = code
        self.path = path


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key!r}")
        result[key] = value
    return result


def finite_json_number(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    return value


def parse_json(text):
    return json.loads(
        text,
        object_pairs_hook=unique_json_object,
        parse_float=finite_json_number,
        parse_constant=finite_json_number,
    )


def load(path):
    return parse_json(Path(path).read_text(encoding="utf-8"))


@lru_cache
def schema():
    return load(ROOT / "schemas/entity.schema.json")


@lru_cache
def base_schema():
    return load(ROOT / "tools/upstream/schemas/task-bundle.schema.json")


def shape(value, definition):
    checker = Draft202012Validator(
        {**schema(), "$ref": "#/$defs/" + definition}, format_checker=FORMATS
    )
    error = next(checker.iter_errors(value), None)
    if error:
        raise ContractError("INVALID_" + definition.upper(), error.json_path, error.message)


def asset_key(ref):
    return ref["asset_id"], ref["version"]


def record_ref(record, kind):
    return {key: record[key] for key in ("namespace", kind + "_id", "version")}


def check_reference_types(value, path=""):
    # The upstream validator resolves references inside open extensions as well
    # as native fields. Check their types before they can be used as lookup keys.
    if isinstance(value, dict):
        if value.keys() == {"asset_id", "version"}:
            for key in ("asset_id", "version"):
                if not isinstance(value[key], str) or not value[key]:
                    raise ContractError("SCHEMA_INVALID", f"{path}/{key}",
                                        "Asset reference IDs and versions must be non-empty strings")
        for key, child in value.items():
            segment = key.replace("~", "~0").replace("/", "~1")
            check_reference_types(child, f"{path}/{segment}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            check_reference_types(child, f"{path}/{index}")


def check(bundle):
    check_reference_types(bundle)
    upstream = validate_bundle(bundle, base_schema())
    if upstream:
        error = upstream[0]
        raise ContractError(error["code"], error["path"], error["message"])
    ext = bundle["extensions"].get("hyper_entity")
    shape(ext, "sampleExtension")
    assets = {asset_key(row): row for row in bundle["assets"]}
    entity, claim = ext["entities"][0], ext["claims"][0]
    if claim["subject"] != record_ref(entity, "entity"):
        raise ContractError("ENTITY_UNRESOLVED", "claims/0/subject", "Unknown entity snapshot")

    bindings = {item["kind"]: item for item in ext["snapshot_bindings"]}
    for kind, row in (("entity", entity), ("claim", claim)):
        binding = bindings.get(kind)
        if not binding or binding["ref"] != record_ref(row, kind):
            raise ContractError("BINDING_UNRESOLVED", "snapshot_bindings", kind)
        asset = assets[asset_key(binding["asset_ref"])]
        if asset["asset_type"] != kind + "_snapshot" or asset["availability"] != "available":
            raise ContractError("SNAPSHOT_UNAVAILABLE", "snapshot_bindings", kind)
        try:
            snapshot = parse_json(asset["content"] or "")
        except ValueError:
            snapshot = None
        # JSON text comparison normalizes object ordering and distinguishes booleans from numbers.
        if json.dumps(snapshot, sort_keys=True) != json.dumps(row, sort_keys=True):
            raise ContractError("SNAPSHOT_MISMATCH", "snapshot_bindings", kind)

    for asset in assets.values():
        if asset["availability"] == "available":
            content = asset["content"]
            if content is None or hashlib.sha256(content.encode()).hexdigest() != asset["sha256"]:
                raise ContractError("CONTENT_DIGEST_MISMATCH", "assets", asset["asset_id"])

    selected = [p for p in bundle["policy_packs"] if
                (p["policy_id"], p["version"], p["adoption"]) ==
                (PROFILE["id"], PROFILE["version"], "selected_for_task")]
    if not selected or not any(r["rule_id"] == "sample-record-match" and r["kind"] == "hard_gate"
                               for r in selected[0]["rules"]):
        raise ContractError("PROFILE_UNBOUND", "policy_packs", "Expected selected sample profile and rule")
    claim_asset = bindings["claim"]["asset_ref"]
    if not any(claim_asset in d["subject_refs"] and PROFILE in d["policy_refs"]
               for d in bundle["decisions"]):
        raise ContractError("DECISION_UNBOUND", "decisions", "Expected a sender decision for this claim")
    return ext, claim, assets


def judgment(claim, assets, scope, purpose):
    if purpose != PURPOSE:
        return "needs_review", "unsupported_purpose"
    if scope != "sample":
        return "needs_evidence", "sampling_basis_missing"
    supported = False
    missing_reasons = set()
    for link in claim["evidence"]:
        if link["relation"] == "context":
            continue
        asset = assets[asset_key(link["asset_ref"])]
        if asset["availability"] != "available":
            missing_reasons.add("evidence_unavailable")
            continue
        try:
            raw = parse_json(asset["content"])
        except (TypeError, ValueError):
            raw = None
        if not isinstance(raw, dict) or asset["asset_type"] != "inspection_record":
            missing_reasons.add("unusable_source_record")
            continue
        expected = {"record_id": link["locator"]["value"], "subject": claim["subject"],
                    **claim["scope"], **claim["value"]}
        if any(key not in raw for key in expected):
            missing_reasons.add("incomplete_source_record")
            continue
        # Python compares True equal to 1. A Boolean never represents a measurement.
        if any(isinstance(raw[key], bool) for key in ("number", "temperature_c")):
            return "needs_review", "evidence_conflict"
        if link["relation"] == "contradicts" or any(raw[k] != v for k, v in expected.items()):
            return "needs_review", "evidence_conflict"
        supported = True
    for reason in ("evidence_unavailable", "unusable_source_record", "incomplete_source_record"):
        if reason in missing_reasons:
            return "needs_evidence", reason
    if not supported:
        return "needs_evidence", "support_missing"
    return "meets_conditions", "verified_sample_record"


def instant(text):
    return datetime.fromisoformat(text.upper().replace("Z", "+00:00"))


def authorization(ext, claim, host, request):
    if host["registry_status"] != "available":
        return "unknown", "registry_unavailable"
    ref = request["grant_ref"]
    if ref not in ext["grant_refs"]:
        return "denied", "grant_not_referenced"
    # A current registry view has one effective row per logical grant identity.
    # If it supplies competing versions, the host must resolve them before use.
    rows = [r for r in host["grants"] if r["grant"]["namespace"] == ref["namespace"]
            and r["grant"]["grant_id"] == ref["grant_id"]]
    if len(rows) != 1 or record_ref(rows[0]["grant"], "grant") != ref or rows[0]["status"] == "unknown":
        return "unknown", "grant_state_unknown"
    if rows[0]["status"] == "revoked":
        return "denied", "revoked"
    grant = rows[0]["grant"]
    start, end = instant(grant["not_before"]), instant(grant["expires_at"])
    if start >= end:
        return "unknown", "invalid_grant_window"
    if not start <= instant(host["now"]) < end:
        return "denied", "outside_grant_window"
    # In this profile the namespace authority issues direct grants. Third-party
    # issuers and delegated grants need a separate, trusted host integration.
    if grant["issuer"] != claim["namespace"] or grant["grantee"] != host["principal"]:
        return "denied", "principal_or_issuer_mismatch"
    if request["purpose"] != grant["purpose"] or request["destination"] != grant["destination"]:
        return "denied", "purpose_or_destination_mismatch"
    if request["action"] not in grant["actions"]:
        return "denied", "action_not_granted"
    for resource in grant["resources"]:
        if resource["claim_ref"] == request["claim_ref"] and set(request["fields"]) <= set(resource["fields"]):
            if not set(request["fields"]) <= {"predicate", "value", "scope", "origin_kind"}:
                return "denied", "field_outside_profile"
            return "granted", "active_scoped_grant"
    return "denied", "resource_or_field_mismatch"


def decide(bundle, host, request):
    ext, claim, assets = check(bundle)
    shape(host, "host")
    shape(request, "request")
    if request["claim_ref"] != record_ref(claim, "claim"):
        raise ContractError("CLAIM_UNRESOLVED", "request/claim_ref", "Unknown claim version")
    status, evidence_reason = judgment(claim, assets, request["scope"], request["purpose"])
    auth, auth_reason = authorization(ext, claim, host, request)
    result = {"judgment": status, "authorization": auth, "execution": "blocked",
              "reason": auth_reason, "view": None}
    if auth == "granted":
        result["reason"] = evidence_reason
        if status == "meets_conditions":
            if request["action"] == "read":
                result.update(execution="read", view={key: claim[key] for key in request["fields"]})
            else:
                result["reason"] = "action_outside_receiver_profile"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").add_argument("bundle")
    decision = commands.add_parser("decide")
    decision.add_argument("bundle")
    decision.add_argument("--host", required=True, help="Trusted local synthetic host fixture")
    decision.add_argument("--request", required=True)
    args = parser.parse_args()
    try:
        bundle = load(args.bundle)
        if args.command == "decide":
            result = decide(bundle, load(args.host), load(args.request))
        else:
            check(bundle)
            result = {"valid": True, "profile": PROFILE}
    except ContractError as exc:
        print(json.dumps({"valid": False, "code": exc.code, "path": exc.path, "detail": str(exc)}))
        return 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"valid": False, "code": "INPUT_UNAVAILABLE", "detail": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
