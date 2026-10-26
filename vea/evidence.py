"""Load an evidence package and verify it cryptographically.

The package is a zip exported by TrustEvidence (format `trustevidence.evidence-proofs.v1`): a manifest of SHA-256
hashes, one folder per evidence record (`record.json` + `content.json`), Merkle inclusion proofs (`proofs.json`)
and a readiness report (`report.json`). This module re-implements the published verification recipe from scratch:

* manifest: every file's SHA-256 matches `manifest.json`;
* content: SHA-256(content.json) equals the record's `content_sha256`;
* chain: SHA-256("<prev_chain_hash or GENESIS>|<content_sha256>|<id>|<collected_at>") equals `chain_hash`;
* inclusion: the record's leaf, SHA-256(0x00 || SHA-256(record.json)), is in the sealed batch's RFC 9162 Merkle tree.

The agent may only cite records that pass all four checks.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def verify_inclusion(leaf_hash: bytes, index: int, size: int, path: list[bytes], root: bytes) -> bool:
    """RFC 9162 section 2.1.3.2: is `leaf_hash` at `index` in the tree of `size` leaves with this `root`?"""
    if index >= size:
        return False
    fn, sn, r = index, size - 1, leaf_hash
    for p in path:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = _node(p, r)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = _node(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root


@dataclass
class Verification:
    content: bool
    chain: bool
    inclusion: bool

    @property
    def ok(self) -> bool:
        return self.content and self.chain and self.inclusion

    def describe(self) -> str:
        if self.ok:
            return "verified (content hash, hash chain, Merkle inclusion)"
        failed = [n for n, v in (("content hash", self.content), ("hash chain", self.chain), ("Merkle inclusion", self.inclusion)) if not v]
        return "FAILED: " + ", ".join(failed)


@dataclass
class Evidence:
    id: str
    record: dict[str, Any]
    content: dict[str, Any]
    path: str
    verification: Verification

    @property
    def title(self) -> str:
        return self.record.get("title", "")

    @property
    def control_ids(self) -> list[str]:
        return list(self.record.get("control_ids") or [])

    @property
    def short_hash(self) -> str:
        return self.record.get("content_sha256", "")[:12]

    def summary(self) -> dict[str, Any]:
        """A compact, model-friendly view: what was checked, the result, and what was observed."""
        c, r = self.content, self.record
        out: dict[str, Any] = {"evidence_id": self.id, "title": self.title, "source": r.get("source_type"),
                               "collected_at": r.get("collected_at"), "controls": self.control_ids,
                               "content_sha256": r.get("content_sha256"), "verification": self.verification.describe()}
        if "check" in c:
            out["check"] = {"title": c["check"].get("title"), "expected": c["check"].get("expected")}
            out["result"] = c.get("result")
            out["resources"] = [{"id": x.get("id"), "status": x.get("status"), "observed": x.get("observed")}
                                for x in (c.get("resources") or [])[:12]]
        else:  # human review or uploaded document
            out["details"] = {k: v for k, v in c.items() if k not in ("documents",)}
            if c.get("documents"):
                out["documents"] = [{"filename": d.get("filename"), "sha256": d.get("sha256")} for d in c["documents"]]
        return out

    def text(self) -> str:
        """Searchable text: title, check, expectation, result and observed values."""
        return json.dumps(self.summary(), ensure_ascii=False).lower()


@dataclass
class EvidencePackage:
    organization: str
    period: dict[str, Any]
    evidence: dict[str, Evidence]
    controls: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    manifest_ok: bool
    manifest_problems: list[str] = field(default_factory=list)

    @property
    def verified_count(self) -> int:
        return sum(1 for e in self.evidence.values() if e.verification.ok)

    def get(self, evidence_id: str) -> Evidence | None:
        return self.evidence.get(evidence_id)


def load(path: str | Path) -> EvidencePackage:
    """Read and verify an evidence package zip. Never raises on tampered content: problems are reported."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        raw = {n: z.read(n) for n in names}

    manifest = json.loads(raw["manifest.json"])
    problems = []
    for name, info in manifest.get("files", {}).items():
        if name not in raw:
            problems.append(f"missing file: {name}")
        elif sha256(raw[name]) != info.get("sha256"):
            problems.append(f"hash mismatch: {name}")

    proofs = json.loads(raw["proofs.json"])
    roots = {}
    for b in proofs.get("batches", []):
        st = json.loads(b["statement"])
        roots[b["batch_index"]] = (bytes.fromhex(st["root_sha256"]), st["leaf_count"])
    proof_by_id = {p["evidence_id"]: p for p in proofs.get("records", [])}

    evidence: dict[str, Evidence] = {}
    for name in sorted(n for n in names if n.endswith("/record.json")):
        folder = name[: -len("record.json")]
        record_bytes = raw[name]
        record = json.loads(record_bytes)
        content_bytes = raw.get(folder + "content.json", b"")
        content = json.loads(content_bytes) if content_bytes else {}

        content_ok = sha256(content_bytes) == record.get("content_sha256")
        prev = record.get("prev_chain_hash") or "GENESIS"
        chain_input = f"{prev}|{record.get('content_sha256')}|{record.get('id')}|{record.get('collected_at')}"
        chain_ok = sha256(chain_input.encode("utf-8")) == record.get("chain_hash")
        inclusion_ok = False
        proof = proof_by_id.get(record.get("id"))
        if proof and proof.get("batch_index") in roots:
            root, size = roots[proof["batch_index"]]
            leaf = hashlib.sha256(b"\x00" + hashlib.sha256(record_bytes).digest()).digest()
            inclusion_ok = verify_inclusion(leaf, proof["leaf_index"], size, [bytes.fromhex(h) for h in proof["audit_path"]], root)

        evidence[record["id"]] = Evidence(record["id"], record, content, folder.rstrip("/"),
                                          Verification(content_ok, chain_ok, inclusion_ok))

    report = json.loads(raw.get("report.json", b"{}"))
    org = report.get("organization") or manifest.get("organization") or {}
    return EvidencePackage(
        organization=org.get("name", "") if isinstance(org, dict) else str(org),
        period=report.get("period") or manifest.get("period") or {},
        evidence=evidence,
        controls=report.get("controls", []),
        findings=report.get("findings", []),
        manifest_ok=not problems,
        manifest_problems=problems,
    )
