"""Load an evidence package and verify it cryptographically.

The package is a zip exported by TrustEvidence (format `trustevidence.evidence-proofs.v1`): a manifest of SHA-256
hashes, one folder per evidence record (`record.json` + `content.json`), Merkle inclusion proofs (`proofs.json`)
and a readiness report (`report.json`). This module re-implements the published verification recipe from scratch:

* manifest: every file's SHA-256 matches `manifest.json`;
* content: SHA-256(content.json) equals the record's `content_sha256`;
* chain: SHA-256("<prev_chain_hash or GENESIS>|<content_sha256>|<id>|<collected_at>") equals `chain_hash`;
* inclusion: the record's leaf, SHA-256(0x00 || SHA-256(record.json)), is in the sealed batch's RFC 9162 Merkle tree.

The checks live in `vea/proofcheck.py`, which is also shipped inside every proof pack. The agent may only cite
records that pass all four.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vea.proofcheck import batch_roots, check_record, sha256, verify_inclusion  # noqa: F401  (one implementation)


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
    proof: dict[str, Any] = field(default_factory=dict, repr=False)
    raw_record: bytes = field(default=b"", repr=False)
    raw_content: bytes = field(default=b"", repr=False)

    @property
    def title(self) -> str:
        return self.record.get("title", "")

    @property
    def control_ids(self) -> list[str]:
        return list(self.record.get("control_ids") or [])

    @property
    def check_key(self) -> str:
        """What this record is evidence *of*: the same check on the same account, re-run on every scan."""
        r = self.record
        if r.get("check_id"):
            scope = r.get("account_scope") or {}
            return f"{r['check_id']}@{scope.get('provider', '')}:{scope.get('account', '')}" if isinstance(scope, dict) else r["check_id"]
        return f"{r.get('source_type')}:{self.title}"

    @property
    def result(self) -> Any:
        r = self.content.get("result")
        return r.get("status") if isinstance(r, dict) else r

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
            out["result"] = self.result
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
    batches: list[dict[str, Any]] = field(default_factory=list, repr=False)
    source_manifest_sha256: str = ""
    latest: dict[str, str] = field(default_factory=dict)  # check_key -> id of the newest record of that check

    def __post_init__(self) -> None:
        newest: dict[str, Evidence] = {}
        for e in self.evidence.values():
            cur = newest.get(e.check_key)
            if cur is None or (e.record.get("collected_at", ""), e.record.get("sequence", 0)) > (
                    cur.record.get("collected_at", ""), cur.record.get("sequence", 0)):
                newest[e.check_key] = e
        self.latest = {k: e.id for k, e in newest.items()}

    @property
    def verified_count(self) -> int:
        return sum(1 for e in self.evidence.values() if e.verification.ok)

    def get(self, evidence_id: str) -> Evidence | None:
        return self.evidence.get(evidence_id)

    def superseded_by(self, evidence_id: str) -> Evidence | None:
        """The newer record of the same check, if this one is not the latest; otherwise None."""
        e = self.get(evidence_id)
        if e is None:
            return None
        newest = self.latest.get(e.check_key)
        return self.get(newest) if newest and newest != evidence_id else None


def load(path: str | Path | bytes) -> EvidencePackage:
    """Read and verify an evidence package zip. Never raises on tampered content: problems are reported."""
    with zipfile.ZipFile(io.BytesIO(path) if isinstance(path, bytes) else path) as z:
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
    roots = batch_roots(proofs.get("batches", []))
    proof_by_id = {p["evidence_id"]: p for p in proofs.get("records", [])}

    evidence: dict[str, Evidence] = {}
    for name in sorted(n for n in names if n.endswith("/record.json")):
        folder = name[: -len("record.json")]
        record_bytes, content_bytes = raw[name], raw.get(folder + "content.json", b"")
        record = json.loads(record_bytes)
        content = json.loads(content_bytes) if content_bytes else {}
        proof = proof_by_id.get(record.get("id")) or {}
        evidence[record["id"]] = Evidence(record["id"], record, content, folder.rstrip("/"),
                                          Verification(*check_record(record_bytes, content_bytes, proof, roots)),
                                          proof=proof, raw_record=record_bytes, raw_content=content_bytes)

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
        batches=proofs.get("batches", []),
        source_manifest_sha256=sha256(raw["manifest.json"]),
    )
