// A separate reader of the frozen synthetic bundle. It never serves external requests.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmdirSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual } from "node:util";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const bundlePath = join(root, "examples", "sample-bundle.json");
const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));
const bundle = readJson(bundlePath);
const host = readJson(join(root, "examples", "host-state.json"));
const request = readJson(join(root, "examples", "read-request.json"));
assert.equal(bundle.nature, "synthetic_example");
const extension = bundle.extensions.hyper_entity;
assert.deepEqual(extension.profile, { id: "inspection.sample.density", version: "1" });
assert.equal(extension.version, "0.2-draft");

function resolve(records, ref) {
  const matches = records.filter((record) =>
    Object.entries(ref).every(([key, value]) => record[key] === value));
  assert.equal(matches.length, 1, "A reference must resolve to one version");
  return matches[0];
}

function readAsset(ref) {
  const asset = resolve(bundle.assets, ref);
  assert.equal(asset.availability, "available");
  assert.equal(createHash("sha256").update(asset.content, "utf8").digest("hex"), asset.sha256);
  return JSON.parse(asset.content);
}

function readSnapshot(kind, ref) {
  const bindings = extension.snapshot_bindings.filter((binding) =>
    binding.kind === kind && isDeepStrictEqual(binding.ref, ref));
  assert.equal(bindings.length, 1);
  return readAsset(bindings[0].asset_ref);
}

const claim = readSnapshot("claim", request.claim_ref);
assert.deepEqual(claim, resolve(extension.claims, request.claim_ref));
const subject = readSnapshot("entity", claim.subject);
assert.deepEqual(subject, resolve(extension.entities, claim.subject));
assert.equal(subject.entity_type, "sample");
assert.equal(claim.predicate, extension.profile.id);
const supporting = claim.evidence.filter((item) => item.relation === "supports");
assert.equal(supporting.length, 1);
const [binding] = supporting;
const source = readAsset(binding.asset_ref);
assert.deepEqual(binding.locator, { kind: "record_id", value: source.record_id });
assert.deepEqual(source.subject, claim.subject);
assert.deepEqual(claim.value, { number: source.number, unit: source.unit });
for (const key of ["method_id", "method_version", "temperature_c"]) {
  assert.equal(claim.scope[key], source[key]);
}
assert.deepEqual(claim.value, { number: 1.04, unit: "g/cm³" });

// Independently reconstructed source data supplies the expected read result.
// Each further case records the expected disposition before calling the Python CLI.
const cases = [
  ["read", {}, {}, "granted", "read", { value: claim.value }],
  ["disclose", {}, { action: "disclose" }, "denied", "blocked", null],
  ["revoked", { grants: host.grants.map((row) => ({ ...row, status: "revoked" })) }, {}, "denied", "blocked", null],
  ["expiry_boundary", { now: host.grants[0].grant.expires_at }, {}, "denied", "blocked", null],
  ["registry_unavailable", { registry_status: "unavailable" }, {}, "unknown", "blocked", null],
];

const scratch = mkdtempSync(join(tmpdir(), "hyper-entity-readback-"));
const hostPath = join(scratch, "host.json");
const requestPath = join(scratch, "request.json");
const results = [];
try {
  for (const [id, hostPatch, requestPatch, auth, execution, view] of cases) {
    writeFileSync(hostPath, JSON.stringify({ ...host, ...hostPatch }));
    writeFileSync(requestPath, JSON.stringify({ ...request, ...requestPatch }));
    const result = spawnSync(process.env.PYTHON || "python", [
      "-B", "-X", "utf8", join(root, "tools", "entity.py"), "decide", bundlePath,
      "--host", hostPath, "--request", requestPath,
    ], { encoding: "utf8" });
    if (result.error) throw result.error;
    assert.equal(result.status, 0, id + ": " + (result.stderr || result.stdout));
    const receipt = JSON.parse(result.stdout);
    assert.equal(receipt.authorization, auth, id);
    assert.equal(receipt.execution, execution, id);
    assert.deepEqual(receipt.view, view, id);
    results.push({ case: id, authorization: auth, execution });
  }
} finally {
  for (const path of [hostPath, requestPath]) {
    try { unlinkSync(path); } catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  rmdirSync(scratch);
}
console.log(JSON.stringify({
  fixture: bundle.bundle_id, source: binding.asset_ref, locator: binding.locator,
  sample: claim.subject, checks: results,
}, null, 2));
