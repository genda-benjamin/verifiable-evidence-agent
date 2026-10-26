"""Verify evidence records and proof packs. Standalone: Python standard library only, no other files needed.

This file is copied into every proof pack as `verify_pack.py`, so the recipient of an answered questionnaire can
check every citation without installing anything or trusting the sender's software:

    python verify_pack.py answered-questionnaire.zip

For each cited evidence record it checks:

* content:   SHA-256(content.json) equals the record's content_sha256;
* chain:     SHA-256("<prev_chain_hash or GENESIS>|<content_sha256>|<id>|<collected_at>") equals chain_hash;
* inclusion: the leaf SHA-256(0x00 || SHA-256(record.json)) is in the sealed batch's RFC 9162 Merkle tree,
             whose root is in the batch statement;

and that every claim cites only records in the pack, and every file matches the pack manifest.
What it cannot check on its own: that the batch root is the one the company really sealed. That needs the batch
signature (when present) or the root obtained from the company through a channel you trust.
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def leaf_hash(record_bytes: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + hashlib.sha256(record_bytes).digest()).digest()


def verify_inclusion(leaf: bytes, index: int, size: int, path: list[bytes], root: bytes) -> bool:
    """RFC 9162 section 2.1.3.2: is `leaf` at `index` in the tree of `size` leaves with this `root`?"""
    if index >= size:
        return False
    fn, sn, r = index, size - 1, leaf
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


def batch_roots(batches: list[dict[str, Any]]) -> dict[int, tuple[bytes, int]]:
    """batch_index -> (root, leaf_count), read from each batch's statement."""
    roots = {}
    for b in batches:
        st = json.loads(b["statement"]) if isinstance(b["statement"], str) else b["statement"]
        roots[b["batch_index"]] = (bytes.fromhex(st["root_sha256"]), st["leaf_count"])
    return roots


def check_record(record_bytes: bytes, content_bytes: bytes, proof: dict[str, Any] | None,
                 roots: dict[int, tuple[bytes, int]]) -> tuple[bool, bool, bool]:
    """(content ok, chain ok, inclusion ok) for one evidence record."""
    record = json.loads(record_bytes)
    content_ok = sha256(content_bytes) == record.get("content_sha256")
    prev = record.get("prev_chain_hash") or "GENESIS"
    chain_ok = sha256(f"{prev}|{record.get('content_sha256')}|{record.get('id')}|{record.get('collected_at')}".encode()) == record.get("chain_hash")
    inclusion_ok = False
    if proof and proof.get("batch_index") in roots and proof.get("evidence_id") == record.get("id"):
        root, size = roots[proof["batch_index"]]
        inclusion_ok = verify_inclusion(leaf_hash(record_bytes), proof["leaf_index"], size,
                                        [bytes.fromhex(h) for h in proof["audit_path"]], root)
    return content_ok, chain_ok, inclusion_ok


def _read_pack(path: Path) -> dict[str, bytes]:
    if path.is_dir():
        return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}


def verify_pack(path: str | Path) -> dict[str, Any]:
    """Verify a proof pack (zip or folder). Returns a report; report["ok"] is the overall verdict."""
    files = _read_pack(Path(path))
    problems: list[str] = []
    manifest = json.loads(files.get("manifest.json", b"{}"))
    for name, digest in (manifest.get("files") or {}).items():
        if name not in files:
            problems.append(f"manifest lists a missing file: {name}")
        elif sha256(files[name]) != digest:
            problems.append(f"file changed after the pack was made: {name}")
    for name in files:
        if name != "manifest.json" and name not in (manifest.get("files") or {}):
            problems.append(f"file not in the manifest: {name}")

    answers = json.loads(files.get("answers.json", b"{}"))
    roots = batch_roots(answers.get("batches") or [])
    records: dict[str, dict[str, Any]] = {}
    for ev in answers.get("evidence") or []:
        rb, cb = files.get(ev["path"] + "/record.json"), files.get(ev["path"] + "/content.json")
        if rb is None or cb is None:
            problems.append(f"{ev['evidence_id']}: record files missing from the pack")
            continue
        c, ch, inc = check_record(rb, cb, ev, roots)
        records[ev["evidence_id"]] = {"content": c, "chain": ch, "inclusion": inc, "ok": c and ch and inc,
                                      "title": json.loads(rb).get("title")}
        for label, ok in (("content hash", c), ("hash chain", ch), ("Merkle inclusion", inc)):
            if not ok:
                problems.append(f"{ev['evidence_id']}: {label} check FAILED")

    claims = 0
    for q in answers.get("questions") or []:
        for claim in q.get("claims") or []:
            claims += 1
            for eid in claim.get("evidence_ids") or []:
                if eid not in records:
                    problems.append(f"{q.get('id')}: claim cites {eid}, which is not in the pack")
    signed = [b for b in answers.get("batches") or [] if b.get("signature")]
    return {"ok": not problems, "problems": problems, "questions": len(answers.get("questions") or []), "claims": claims,
            "records": len(records), "records_verified": sum(r["ok"] for r in records.values()),
            "batch_roots": {i: r.hex() for i, (r, _) in roots.items()},
            "signatures": f"{len(signed)} of {len(roots)} batches signed (signature checking is not done by this script)"
                          if signed else "batches are unsigned: confirm the root with the company through a channel you trust"}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python verify_pack.py <proof-pack.zip | folder>")
        return 2
    report = verify_pack(argv[0])
    print(f"questions: {report['questions']}   claims: {report['claims']}   "
          f"evidence records: {report['records_verified']} of {report['records']} verified")
    for i, root in report["batch_roots"].items():
        print(f"batch {i} root: {root}")
    print(f"trust anchor: {report['signatures']}")
    for p in report["problems"]:
        print("PROBLEM:", p)
    print("RESULT:", "VERIFIED" if report["ok"] else "NOT VERIFIED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
