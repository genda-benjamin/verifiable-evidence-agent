"""Build a proof pack: the answered questionnaire plus exactly the evidence it cites, with the proofs.

The recipient (the customer's security team, an auditor) gets one zip:

    answers.html     the answers, readable in any browser; it re-verifies every citation locally when opened
    answers.json     the same answers, machine-readable, with the proofs for each cited record
    evidence/<id>/   record.json + content.json, byte-for-byte as sealed, for each cited record only
    verify_pack.py   a standalone verifier (Python standard library only)
    README.txt       what this is and how to check it
    manifest.json    SHA-256 of every other file

Only cited records are included, so a company shares the evidence behind its answers and nothing else.
"""
from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vea import __version__
from vea.evidence import EvidencePackage
from vea.audit import Approvals
from vea.proofcheck import sha256

FORMAT = "vea.proof-pack.v1"
_HERE = Path(__file__).resolve().parent


class PackError(ValueError):
    pass


def _answers_doc(package: EvidencePackage, results: list[dict[str, Any]], allow_drafts: bool,
                 log: Approvals) -> tuple[dict[str, Any], list[str]]:
    unreviewed = [r.get("id", "?") for r in results if r.get("status") not in ("approved", "rejected")]
    if unreviewed and not allow_drafts:
        raise PackError(f"{len(unreviewed)} answer(s) are not reviewed yet ({', '.join(unreviewed[:8])}). "
                        "Approve or reject them first, or export with drafts allowed (they are marked DRAFT).")
    shipped = [r for r in results if r.get("status") != "rejected"]

    cited: list[str] = []
    for r in shipped:
        for c in r.get("claims") or []:
            for eid in c.get("evidence_ids") or []:
                if eid not in cited:
                    cited.append(eid)
    evidence = []
    for eid in cited:
        e = package.get(eid)
        if e is None or not e.verification.ok:
            raise PackError(f"cited evidence {eid} is missing or fails verification; it cannot be shipped")
        evidence.append({"evidence_id": e.id, "path": f"evidence/{e.id}", "title": e.title,
                         "batch_index": e.proof.get("batch_index"), "leaf_index": e.proof.get("leaf_index"),
                         "sequence": e.proof.get("sequence"), "audit_path": e.proof.get("audit_path")})
    used_batches = {ev["batch_index"] for ev in evidence}

    questions = [{"id": r.get("id"), "question": r.get("question"), "answer": r.get("answer"),
                  "status": r.get("status") if r.get("status") == "approved" else "draft",
                  "caveats": r.get("caveats") or "", "flags": r.get("flags") or [], "missing": r.get("missing") or [],
                  "reviewer_note": r.get("reviewer_note") or "",
                  **({"approval": log.latest_decision(r["id"])} if r.get("status") == "approved" else {}),
                  "claims": [{"text": c.get("text"), "evidence_ids": c.get("evidence_ids"),
                              "verifier_reason": c.get("verifier_reason", "")} for c in r.get("claims") or []]}
                 for r in shipped]
    doc = {
        "format": FORMAT,
        "generator": f"Verifiable Evidence Agent {__version__}",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "organization": package.organization,
        "period": package.period,
        "source_package": {"manifest_sha256": package.source_manifest_sha256},
        "batches": [b for b in package.batches if b.get("batch_index") in used_batches],
        "keys": [k for k in package.keys if k.get("key_id") in {b.get("signing_key_id") for b in package.batches
                                                               if b.get("batch_index") in used_batches}],
        "questions": questions,
        "evidence": evidence,
        "excluded": {"rejected_answers": len(results) - len(shipped)},
    }
    return doc, cited


README = """Answered security questionnaire: {org}
Evidence period: {start} to {end}. Generated {generated} by {generator}.

Every claim in these answers cites evidence records that {org} sealed when they were collected. This pack
contains exactly those records, byte for byte, and the proofs that they are unchanged since sealing.

How to check it
  1. Open answers.html in a browser. It recomputes every hash and Merkle proof on your machine and shows the
     result next to each answer. Nothing is sent anywhere.
  2. Or run the standalone verifier (Python 3, standard library only):  python verify_pack.py <this zip>

What the checks prove
  Each cited record's content matches its sealed hash, its hash-chain link is intact, and it is included in the
  sealed batch whose root is printed by the verifier. Every file matches manifest.json.

Who sealed it
  {anchor}

Answers marked DRAFT were not approved by a person before export.
"""


def build(package: EvidencePackage, results: list[dict[str, Any]], out: str | Path, *, log: Approvals, actor: str,
          allow_drafts: bool = False) -> dict[str, Any]:
    """Write the proof pack zip to `out`, record the export in the audit log, and return the answers document."""
    data, doc = build_bytes(package, results, log=log, actor=actor, allow_drafts=allow_drafts)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return doc


def build_bytes(package: EvidencePackage, results: list[dict[str, Any]], *, log: Approvals, actor: str,
                allow_drafts: bool = False) -> tuple[bytes, dict[str, Any]]:
    """Build the proof pack in memory and record the export; returns the zip and its answers document.

    `log` is where approvals live: the standalone AuditLog, or a host application's audit log (RecordedApprovals).
    Refuses when the log is broken, or when an answer marked approved has no matching approval in it (including an
    answer edited after it was approved)."""
    problems = log.approval_problems([r for r in results if r.get("status") != "rejected"])
    if problems:
        raise PackError("the audit log does not back these answers: " + "; ".join(problems[:5]))
    doc, cited = _answers_doc(package, results, allow_drafts, log)
    files: dict[str, bytes] = {}
    for eid in cited:
        e = package.get(eid)
        files[f"evidence/{eid}/record.json"] = e.raw_record
        files[f"evidence/{eid}/content.json"] = e.raw_content
    answers_bytes = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
    files["answers.json"] = answers_bytes
    files["verify_pack.py"] = (_HERE / "proofcheck.py").read_bytes()

    embedded = {"answers": doc, "files": {k: base64.b64encode(v).decode() for k, v in files.items() if k.startswith("evidence/")}}
    # Escape "<" so no string inside the evidence can close the script element it is embedded in.
    data = json.dumps(embedded, ensure_ascii=False).replace("<", "\\u003c")
    html = (_HERE / "pack_template.html").read_text(encoding="utf-8").replace("/*__PACK_DATA__*/", data)
    files["answers.html"] = html.encode("utf-8")

    signed = doc["batches"] and all(b.get("signature") for b in doc["batches"])
    fingerprints = sorted({k.get("fingerprint_sha256", "?") for k in doc["keys"]})
    files["README.txt"] = README.format(
        org=doc["organization"] or "The company", start=doc["period"].get("start", "?"), end=doc["period"].get("end", "?"),
        generated=doc["generated_at"], generator=doc["generator"],
        anchor=(f"Every sealed batch is signed by the key with SHA-256 fingerprint {', '.join(fingerprints)}. Compare it with "
                "the fingerprint the company publishes, or check it in one step:\n  python verify_pack.py <this zip> --key "
                "<published fingerprint>" if signed else
                "These batches are not signed, so confirm the root printed by the verifier with the company through a "
                "channel you trust (for example, a root published on its website or given in an earlier report).")).encode("utf-8")
    files["manifest.json"] = json.dumps({"format": FORMAT, "hash_algorithm": "SHA-256",
                                         "files": {k: sha256(v) for k, v in sorted(files.items())}}, indent=2).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            z.writestr(name, files[name])
    log.exported(actor, sha256(buf.getvalue()), [q["id"] for q in doc["questions"]])
    return buf.getvalue(), doc
