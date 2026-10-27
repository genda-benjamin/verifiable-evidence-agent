"""Verify evidence records and proof packs. Standalone: Python standard library only, no other files needed.

This file is copied into every proof pack as `verify_pack.py`, so the recipient of an answered questionnaire can
check every citation without installing anything or trusting the sender's software:

    python verify_pack.py answered-questionnaire.zip

For each cited evidence record it checks:

* content:   SHA-256(content.json) equals the record's content_sha256;
* chain:     SHA-256("<prev_chain_hash or GENESIS>|<content_sha256>|<id>|<collected_at>") equals chain_hash;
* inclusion: the leaf SHA-256(0x00 || SHA-256(record.json)) is in the sealed batch's RFC 9162 Merkle tree,
             whose root is in the batch statement;

and that every claim cites only records in the pack, every file matches the pack manifest, and every approved answer
carries an approval record whose fingerprint matches the answer exactly (so it was not edited after approval).
and, when the batches are signed, that each batch statement (which holds the root) carries a valid signature (Ed25519 or
ES256, checked here with the standard library). The trust anchor is the company's signing key: compare the fingerprint
printed with the one the company publishes, or pass it with --key and the check is done for you. Unsigned batches
fall back to confirming the root with the company through a channel you trust.
"""
from __future__ import annotations

import base64
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


# ------------------------------------------------------------------------------------------------ signatures -----
# Batch statements are signed with Ed25519 (RFC 8032) or ES256 (ECDSA P-256 with SHA-256). Verification below uses the
# standard library only, so this file stays a single self-contained verifier. It checks public data, so it does not
# need to be constant-time.

_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_SQRT_M1 = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % _ED_P, (P[1] + P[0]) * (Q[1] + Q[0]) % _ED_P
    C, D = 2 * P[3] * Q[3] * _ED_D % _ED_P, 2 * P[2] * Q[2] % _ED_P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _ED_P, G * H % _ED_P, F * G % _ED_P, E * H % _ED_P)


def _ed_mul(s: int, P):
    Q = (0, 1, 1, 0)  # the neutral element
    while s > 0:
        if s & 1:
            Q = _ed_add(Q, P)
        P, s = _ed_add(P, P), s >> 1
    return Q


def _ed_equal(P, Q) -> bool:
    return (P[0] * Q[2] - Q[0] * P[2]) % _ED_P == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _ED_P == 0


def _ed_x(y: int, sign: int):
    if y >= _ED_P:
        return None
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P:
        x = x * _ED_SQRT_M1 % _ED_P
    if (x * x - x2) % _ED_P:
        return None
    return _ED_P - x if (x & 1) != sign else x


def _ed_point(raw: bytes):
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _ed_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _ED_P)


_ED_GY = 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P
_ED_G = (_ed_x(_ED_GY, 0), _ED_GY, 1, _ed_x(_ED_GY, 0) * _ED_GY % _ED_P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 section 5.1.7."""
    if len(signature) != 64:
        return False
    A, R = _ed_point(public), _ed_point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if A is None or R is None or s >= _ED_L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _ED_L
    return _ed_equal(_ed_mul(s, _ED_G), _ed_add(R, _ed_mul(h, A)))


_P256_P = 2 ** 256 - 2 ** 224 + 2 ** 192 + 2 ** 96 - 1
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P256_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
           0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _p256_add(P, Q):
    if P is None:
        return Q
    if Q is None:
        return P
    if P[0] == Q[0] and (P[1] + Q[1]) % _P256_P == 0:
        return None
    if P == Q:
        lam = (3 * P[0] * P[0] - 3) * pow(2 * P[1], _P256_P - 2, _P256_P) % _P256_P
    else:
        lam = (Q[1] - P[1]) * pow(Q[0] - P[0], _P256_P - 2, _P256_P) % _P256_P
    x = (lam * lam - P[0] - Q[0]) % _P256_P
    return (x, (lam * (P[0] - x) - P[1]) % _P256_P)


def _p256_mul(k: int, P):
    R = None
    while k:
        if k & 1:
            R = _p256_add(R, P)
        P, k = _p256_add(P, P), k >> 1
    return R


def es256_verify(x: int, y: int, message: bytes, signature: bytes) -> bool:
    """ECDSA P-256 with SHA-256 (FIPS 186-4), signature as raw r || s (64 bytes)."""
    if len(signature) != 64 or not (0 < x < _P256_P and 0 < y < _P256_P):
        return False
    if (y * y - (x * x * x - 3 * x + _P256_B)) % _P256_P:
        return False  # the key is not a point on the curve
    r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    if not (0 < r < _P256_N and 0 < s < _P256_N):
        return False
    w = pow(s, _P256_N - 2, _P256_N)
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    X = _p256_add(_p256_mul(e * w % _P256_N, _P256_G), _p256_mul(r * w % _P256_N, (x, y)))
    return X is not None and X[0] % _P256_N == r


def _b64(value: str) -> bytes:
    value = value.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4))


def key_fingerprint(key: dict[str, Any]) -> str | None:
    """SHA-256 of the raw public key (Ed25519: 32 bytes; P-256: 0x04 || x || y), as TrustEvidence publishes it."""
    try:
        if key.get("alg") == "Ed25519":
            return sha256(_b64(key["public_key"]))
        if key.get("alg") == "ES256":
            return sha256(b"\x04" + _b64(key["jwk"]["x"]) + _b64(key["jwk"]["y"]))
    except (KeyError, ValueError, TypeError):
        return None
    return None


def batch_signatures(batches: list[dict[str, Any]], keys: list[dict[str, Any]]) -> dict[int, bool | None]:
    """batch_index -> True (signature valid), False (invalid, or its key is missing or altered), None (unsigned)."""
    by_id = {k.get("key_id"): k for k in keys}
    out: dict[int, bool | None] = {}
    for b in batches:
        if not b.get("signature"):
            out[b["batch_index"]] = None
            continue
        key = by_id.get(b.get("signing_key_id"))
        statement = b["statement"] if isinstance(b["statement"], str) else json.dumps(b["statement"])
        ok = False
        try:
            sig, msg = _b64(b["signature"]), statement.encode("utf-8")
            if key is not None and key.get("fingerprint_sha256") == key_fingerprint(key):
                if b.get("signature_alg") == "Ed25519" == key.get("alg"):
                    ok = ed25519_verify(_b64(key["public_key"]), msg, sig)
                elif b.get("signature_alg") == "ES256" == key.get("alg"):
                    ok = es256_verify(int.from_bytes(_b64(key["jwk"]["x"]), "big"), int.from_bytes(_b64(key["jwk"]["y"]), "big"), msg, sig)
        except (KeyError, ValueError, TypeError):
            ok = False
        out[b["batch_index"]] = ok
    return out


def batch_roots(batches: list[dict[str, Any]]) -> dict[int, tuple[bytes, int]]:
    """batch_index -> (root, leaf_count), read from each batch's statement."""
    roots = {}
    for b in batches:
        st = json.loads(b["statement"]) if isinstance(b["statement"], str) else b["statement"]
        roots[b["batch_index"]] = (bytes.fromhex(st["root_sha256"]), st["leaf_count"])
    return roots


def canonical(obj: Any) -> bytes:
    """The exact bytes that get hashed: sorted keys, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def answer_digest(answer: dict[str, Any]) -> str:
    """Fingerprint of what a reviewer approves: the question, the answer, every claim with its citations, the caveats
    and what is missing. Changing any of it after approval changes the digest."""
    return sha256(canonical({
        "question": answer.get("question"), "answer": answer.get("answer"),
        "claims": [{"text": c.get("text"), "evidence_ids": list(c.get("evidence_ids") or [])} for c in answer.get("claims") or []],
        "caveats": answer.get("caveats") or "", "missing": list(answer.get("missing") or []),
    }))


def entry_hash(entry: dict[str, Any]) -> str:
    """Hash of an audit-log entry over every field except the hash itself (so it covers the link to the previous one)."""
    return sha256(canonical({k: v for k, v in entry.items() if k != "hash"}))


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


def verify_pack(path: str | Path, trusted_key: str | None = None) -> dict[str, Any]:
    """Verify a proof pack (zip or folder). Returns a report; report["ok"] is the overall verdict.

    `trusted_key` is the SHA-256 fingerprint of the company's published signing key, if the recipient has it: then
    every batch must be signed by exactly that key, which settles the trust anchor without comparing roots."""
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
    sigs = batch_signatures(answers.get("batches") or [], answers.get("keys") or [])
    for i, state in sigs.items():
        if state is False:
            problems.append(f"batch {i}: signature INVALID (the sealed root was changed, or the key in the pack was altered)")
    records: dict[str, dict[str, Any]] = {}
    for ev in answers.get("evidence") or []:
        rb, cb = files.get(ev["path"] + "/record.json"), files.get(ev["path"] + "/content.json")
        if rb is None or cb is None:
            problems.append(f"{ev['evidence_id']}: record files missing from the pack")
            continue
        c, ch, inc = check_record(rb, cb, ev, roots)
        signed_ok = sigs.get(ev.get("batch_index")) is not False
        records[ev["evidence_id"]] = {"content": c, "chain": ch, "inclusion": inc, "signature": sigs.get(ev.get("batch_index")),
                                      "ok": c and ch and inc and signed_ok, "title": json.loads(rb).get("title")}
        for label, ok in (("content hash", c), ("hash chain", ch), ("Merkle inclusion", inc)):
            if not ok:
                problems.append(f"{ev['evidence_id']}: {label} check FAILED")

    claims = approved = 0
    for q in answers.get("questions") or []:
        for claim in q.get("claims") or []:
            claims += 1
            for eid in claim.get("evidence_ids") or []:
                if eid not in records:
                    problems.append(f"{q.get('id')}: claim cites {eid}, which is not in the pack")
        if q.get("status") == "approved":
            entry = q.get("approval") or {}
            if not entry:
                problems.append(f"{q.get('id')}: marked approved but carries no approval record")
            elif entry.get("hash") != entry_hash(entry) or entry.get("action") != "approved" or entry.get("subject") != q.get("id"):
                problems.append(f"{q.get('id')}: approval record is altered or does not belong to this answer")
            elif (entry.get("details") or {}).get("digest") != answer_digest(q):
                problems.append(f"{q.get('id')}: answer changed after it was approved")
            else:
                approved += 1
    keys = {k.get("key_id"): k for k in answers.get("keys") or []}
    signers = {b.get("signing_key_id") for b in answers.get("batches") or [] if b.get("signature")}
    fingerprints = sorted({keys[k].get("fingerprint_sha256", "?") for k in signers if k in keys})
    if any(state is None for state in sigs.values()):
        anchor = "some batches are unsigned: confirm their roots with the company through a channel you trust"
    elif all(sigs.values()):
        anchor = (f"every batch is signed by the key with fingerprint {', '.join(fingerprints)}: compare it with the "
                  f"fingerprint the company publishes, or pass it with --key")
    else:
        anchor = "batch signatures failed: do not rely on this pack"
    if trusted_key:
        wanted = trusted_key.strip().lower()
        if any(state is not True for state in sigs.values()) or fingerprints != [wanted]:
            problems.append(f"not signed only by the key you trust ({wanted[:16]}...): signed by {fingerprints or 'no key'}")
        else:
            anchor = f"every batch is signed by the key you trust ({wanted[:16]}...)"
    return {"ok": not problems, "problems": problems, "questions": len(answers.get("questions") or []), "claims": claims,
            "approvals_verified": approved,
            "records": len(records), "records_verified": sum(r["ok"] for r in records.values()),
            "batch_roots": {i: r.hex() for i, (r, _) in roots.items()}, "batch_signatures": sigs,
            "key_fingerprints": fingerprints, "signatures": anchor}


def main(argv: list[str]) -> int:
    trusted = None
    if "--key" in argv:
        i = argv.index("--key")
        trusted = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 1 or trusted == "":
        print("usage: python verify_pack.py <proof-pack.zip | folder> [--key <the company's published key fingerprint>]")
        return 2
    report = verify_pack(argv[0], trusted)
    print(f"questions: {report['questions']}   claims: {report['claims']}   "
          f"evidence records: {report['records_verified']} of {report['records']} verified   "
          f"approvals bound to their answers: {report['approvals_verified']}")
    for i, root in report["batch_roots"].items():
        print(f"batch {i} root: {root}")
    print(f"trust anchor: {report['signatures']}")
    for p in report["problems"]:
        print("PROBLEM:", p)
    print("RESULT:", "VERIFIED" if report["ok"] else "NOT VERIFIED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
