"""Build samples/sample-co-tampered.zip: the Sample Co package with evidence forged the way a dishonest vendor would.

    python evals/tamper.py

Five sealed records are edited to hide problems, and the export is made to look consistent: the readiness report's
check and control statuses are changed to match, the related findings are marked resolved, and manifest.json and
SHA256SUMS.txt are recomputed. The batch roots and Merkle proofs are left as sealed, because changing them would
change the roots the company already gave its customers.

| Check (latest record)                          | Forgery                                  | What still catches it   |
|------------------------------------------------|------------------------------------------|-------------------------|
| aws.iam.console_users_mfa                       | ben.okafor shown with MFA; FAIL -> PASS  | content hash            |
| aws.ec2.ebs_volumes_encrypted                   | unencrypted volume shown encrypted       | content hash            |
| github.repo.open_critical_dependabot_alerts     | open alerts removed; FAIL -> PASS        | content hash            |
| aws.iam.no_user_admin_policies                  | admin policy removed; FAIL -> PASS, and  | Merkle inclusion only   |
|                                                 | the record's own hashes recomputed       |                         |
| Penetration testing review (TE-VM-03)           | "booked" note replaced by "completed,    | content hash            |
|                                                 | no critical findings"; -> PASS           |                         |

The output is deterministic (fixed zip timestamps), so the committed file can be regenerated and compared.
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vea import evidence  # noqa: E402

SOURCE = ROOT / "samples" / "sample-co-evidence-package.zip"
TARGET = ROOT / "samples" / "sample-co-tampered.zip"
FORGED_CHECKS = ["aws.iam.console_users_mfa", "aws.ec2.ebs_volumes_encrypted", "github.repo.open_critical_dependabot_alerts",
                 "aws.iam.no_user_admin_policies"]
FIX_UP_HASHES = {"aws.iam.no_user_admin_policies"}  # the careful forger also recomputes the record's own hashes
PENTEST_NOTE = ("Independent penetration test completed by an external firm on 2026-09-15 covering all production "
                "systems; no critical or high findings.")


def _dump(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _forge_check(content: dict) -> dict:
    """Every resource passes, with observed values that look like passing ones."""
    for r in content.get("resources") or []:
        if r.get("status") != "PASS":
            r["status"] = "PASS"
            o = r.get("observed") or {}
            if "mfa_active" in o:
                o["mfa_active"] = True
            if "encrypted" in o:
                o["encrypted"], o["kms_key"] = True, "arn:aws:kms:eu-west-1:123456789012:key/5f1c2a9e-0b7d-4c3e-9a8f-1d2e3c4b5a60"
            if "attached_policies" in o:
                o["attached_policies"] = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
            if "open_beyond_sla" in o:
                o["open_beyond_sla"] = []
    n = len(content.get("resources") or [])
    content["result"] = {"status": "PASS", "summary": f"All {n} resources pass. (Sample data.)"}
    return content


def build() -> dict[str, list[str]]:
    package = evidence.load(SOURCE)
    with zipfile.ZipFile(SOURCE) as z:
        files = {i.filename: z.read(i.filename) for i in z.infolist()}

    forged: dict[str, str] = {}  # evidence id -> what was forged
    for check in FORGED_CHECKS:
        e = next(package.get(i) for i in package.latest.values() if package.get(i).record.get("check_id") == check)
        content = _forge_check(json.loads(files[e.path + "/content.json"]))
        files[e.path + "/content.json"] = _dump(content)
        if check in FIX_UP_HASHES:
            record = json.loads(files[e.path + "/record.json"])
            record["content_sha256"] = _sha(files[e.path + "/content.json"])
            record["chain_hash"] = _sha(f"{record.get('prev_chain_hash') or 'GENESIS'}|{record['content_sha256']}|"
                                        f"{record['id']}|{record['collected_at']}".encode())
            files[e.path + "/record.json"] = _dump(record)
        forged[e.id] = check
    pentest = next(e for e in package.evidence.values() if e.record.get("source_type") == "human_review"
                   and "TE-VM-03" in e.control_ids)
    content = json.loads(files[pentest.path + "/content.json"])
    content.update(result="PASS", notes=PENTEST_NOTE)
    files[pentest.path + "/content.json"] = _dump(content)
    forged[pentest.id] = "review:TE-VM-03"

    # Make the readiness report agree with the forged records.
    report = json.loads(files["report.json"])
    for c in report.get("controls", []):
        for x in c.get("checks") or []:
            if x.get("evidence_id") in forged:
                x["status"], x["summary"] = "PASS", "All resources pass. (Sample data.)"
        if (c.get("review") or {}).get("evidence_id") in forged:
            c["review"].update(result="PASS", notes=PENTEST_NOTE)
        if c.get("checks") or c.get("review"):
            statuses = [x.get("status") for x in c.get("checks") or []] + ([c["review"].get("result")] if c.get("review") else [])
            if statuses and all(s == "PASS" for s in statuses):
                c["status"], c["open_findings"] = "PASS", 0
    for f in report.get("findings", []):
        if f.get("latest_evidence_id") in forged or f.get("check_id") in FORGED_CHECKS:
            f["status_at_period_end"] = "resolved"
    files["report.json"] = json.dumps(report, indent=2, sort_keys=True).encode("utf-8")

    # Recompute the manifest and checksums so the package looks untouched.
    manifest = json.loads(files["manifest.json"])
    manifest["files"] = {name: {"sha256": _sha(data), "bytes": len(data)} for name, data in sorted(files.items())
                         if name not in ("manifest.json", "SHA256SUMS.txt", "manifest.sig.json")}
    files["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    files["SHA256SUMS.txt"] = ("".join(f"{v['sha256']}  {k}\n" for k, v in manifest["files"].items())
                               + f"{_sha(files['manifest.json'])}  manifest.json\n").encode("utf-8")

    with zipfile.ZipFile(TARGET, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 27, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[name])
    return {"forged": sorted(forged.values()), "evidence_ids": sorted(forged)}


if __name__ == "__main__":
    out = build()
    print(f"wrote {TARGET.relative_to(ROOT)}: forged {', '.join(out['forged'])}")
