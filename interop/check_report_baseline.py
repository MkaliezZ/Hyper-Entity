"""Replay the Hyper Report baseline locally, using its application and test dependencies.

From the Entity root on this workstation, run in PowerShell:
    & '../消费4.0/report-app/.venv/Scripts/python.exe' -X utf8 interop/check_report_baseline.py

Pass --report-root for another checkout containing report_app/ and tests/test_workbench.py.
The run uses TestClient, a temporary application root and synthetic materials only.
"""

import argparse
import csv
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
from decimal import Decimal
from pathlib import Path
import sys
import socket
from tempfile import TemporaryDirectory
from unittest.mock import patch


ENTITY_ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_measurement():
    raw = (ENTITY_ROOT / "examples" / "sample-bundle.json").read_bytes()
    bundle = json.loads(raw)
    claims = bundle["extensions"]["hyper_entity"]["claims"]
    selected = [c for c in claims if c["claim_id"] == "density-07"]
    require(len(selected) == 1, "Expected one density-07 claim")
    claim = selected[0]
    sample = claim["subject"]["entity_id"]
    number = Decimal(str(claim["value"]["number"]))
    unit = claim["value"]["unit"]
    require((sample, number, unit) == ("sample-07", Decimal("1.04"), "g/cm³"),
            "Frozen comparison input changed")
    rows = io.StringIO(newline="")
    writer = csv.writer(rows)
    writer.writerow(["样品编号", "样品名称", "检测项目", "检测结果", "单位", "检测方法", "指标"])
    writer.writerow([sample, "合成样品", "密度", str(number), unit, claim["scope"]["method_id"], ""])
    return hashlib.sha256(raw).hexdigest(), sample, number, unit, rows.getvalue().encode("utf-8")


def load_helpers(checkout):
    path = checkout / "tests" / "test_workbench.py"
    if not path.is_file() or not (checkout / "report_app" / "api.py").is_file():
        raise ValueError("Choose a Hyper Report source checkout with its test fixtures")
    sys.path.insert(0, str(checkout))
    spec = importlib.util.spec_from_file_location("report_baseline_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replay(checkout, db_root):
    h = load_helpers(checkout)
    digest, sample, number, unit, csv_bytes = read_measurement()
    app = h.create_app(db_root)
    _, workspace = h.user(app.state.store, "author@example.test")
    viewer_id, _ = h.user(app.state.store, "receiver@example.test", "viewer", workspace)
    with h.TestClient(app) as client:
        h.login(client, "author@example.test")
        project = h.check(client.post("/api/projects", json={"name": "Entity baseline", "commission_no": "M1"}))["id"]
        template = h.import_template(client)
        h.upload(client, project, "commission", "commission.txt", "委托单位：合成试验\n报告备注：合成材料".encode())
        material = h.upload(client, project, "records", "measurement.csv", csv_bytes)
        h.upload(client, project, "photo", "placeholder.png", h.image_blob())
        job = h.check(client.post(f"/api/projects/{project}/reports", json=h.payload(
            template["id"], sample=sample, number="ENTITY-DEMO-07",
            row_selection=[{"sample_no": sample, "item": "密度"}])))
        status = h.wait_report(client, project, job["id"])["status"]
        require(status == "done", f"Synthetic report did not finish: {status}")
        exchange = f"/api/internal/reports/{job['report_id']}/versions/{job['version']}"
        query = {"schema_version": 1, "fields": "value"}
        full = h.check(client.get(exchange, params=query))
        require(len(full["rows"]) == 1, "Unexpected number of report rows")
        row = full["rows"][0]
        field = row["fields"]["value"]
        require(set(row["fields"]) == {"value"} and row["item"] == "密度", "Field projection mismatch")
        require((row["sample_no"], Decimal(str(field["value"])), row["unit"]) == (sample, number, unit),
                "Returned result differs from the Entity sample")
        require(full["report_version"] == field["report_version"] == 1, "Returned report version mismatch")
        source = field["source"]
        locator = {"kind": "table", "sheet": "CSV", "row": 2, "column": 4}
        require(source["file_id"] == material["id"] and source["version"] == 1 and source["locator"] == locator,
                "Returned source does not point to the submitted measurement cell")
        source_url = f"/api/projects/{project}/sources/{material['id']}"
        require(h.check(client.get(source_url))["version"] == 1, "Source metadata was not accessible to author")
        h.check(client.put(f"/api/projects/{project}/access/{viewer_id}", json={"actions": ["view"]}))
        client.cookies.clear()
        h.login(client, "receiver@example.test")
        limited = h.check(client.get(exchange, params=query))["rows"][0]["fields"]["value"]
        require(limited["value"] == field["value"] and limited.get("source") is None,
                "View-only projection should preserve the value and hide its source")
        source_status = client.get(source_url).status_code
        require(source_status == 403, "View-only recipient should not receive the source")
    return {
        "result": "passed", "entity_bundle_sha256": digest,
        "report_schema_version": full["schema_version"], "report_version": full["report_version"],
        "sample": sample, "value": str(number), "unit": unit,
        "source_version": source["version"], "source_locator": source["locator"],
        "view_only_has_value": True, "view_only_source_visible": False,
        "view_only_source_status": source_status,
        "scope": "Synthetic fact projection and project/source access only. CSV and Entity locators use different identifiers.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-root", type=Path, default=ENTITY_ROOT.parent / "消费4.0" / "report-app")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        # Windows asyncio's internal wake-up pipe uses a loopback socket pair.
        host = address[0] if isinstance(address, tuple) else ""
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise RuntimeError("This offline baseline accepts loopback sockets only")
        return original_connect(sock, address)

    with TemporaryDirectory(prefix="hyper-entity-baseline-") as directory:
        root = Path(directory)
        env = {"CONSUMER_REPORT_DATA": str(root / "app"), "HYPER_REPORT_ALLOWED_HOSTS": "testserver",
               "HYPER_REPORT_REQUIRE_HTTPS": "0", "HYPER_REPORT_WORKERS": "1"}
        with patch.dict(os.environ, env), patch.object(socket.socket, "connect", local_connect):
            result = replay(args.report_root.resolve(), root / "app")
    require(not root.exists(), "Temporary application files remain")
    result["temporary_store_removed"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
