import hashlib
import json
import zipfile
from pathlib import Path

from vea import evidence

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "sample-co-evidence-package.zip"


def test_sample_package_verifies():
    p = evidence.load(SAMPLE)
    assert p.manifest_ok and p.organization == "Sample Co"
    assert len(p.evidence) == 246 and p.verified_count == 246


def test_edited_content_is_detected(tmp_path):
    src = zipfile.ZipFile(SAMPLE)
    target = next(n for n in src.namelist() if n.endswith("/content.json"))
    out = tmp_path / "tampered.zip"
    with zipfile.ZipFile(out, "w") as z:
        for n in src.namelist():
            data = src.read(n)
            if n == target:
                data = data.replace(b"PASS", b"FAIL", 1) if b"PASS" in data else data + b" "
            z.writestr(n, data)
    p = evidence.load(out)
    assert not p.manifest_ok and any(target in m for m in p.manifest_problems)
    bad = [e for e in p.evidence.values() if not e.verification.ok]
    assert len(bad) == 1 and not bad[0].verification.content


def test_inclusion_proof_small_trees():
    leaves = [hashlib.sha256(b"\x00" + bytes([i])).digest() for i in range(5)]
    node = lambda a, b: hashlib.sha256(b"\x01" + a + b).digest()
    # RFC 9162 tree of 5: root = node(node(node(0,1), node(2,3)), 4)
    root = node(node(node(leaves[0], leaves[1]), node(leaves[2], leaves[3])), leaves[4])
    assert evidence.verify_inclusion(leaves[4], 4, 5, [node(node(leaves[0], leaves[1]), node(leaves[2], leaves[3]))], root)
    assert evidence.verify_inclusion(leaves[2], 2, 5, [leaves[3], node(leaves[0], leaves[1]), leaves[4]], root)
    assert not evidence.verify_inclusion(leaves[2], 3, 5, [leaves[3], node(leaves[0], leaves[1]), leaves[4]], root)


RESEALED = SAMPLE.parent / "sample-co-resealed.zip"


def test_signature_checks_match_the_standards():
    from vea import proofcheck as pc
    # RFC 8032 section 7.1, TEST 2
    pub = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
    sig = bytes.fromhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
    assert pc.ed25519_verify(pub, bytes([0x72]), sig) and not pc.ed25519_verify(pub, bytes([0x73]), sig)
    crypto = __import__("pytest").importorskip("cryptography")  # noqa: F841
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = ec.generate_private_key(ec.SECP256R1())
    r, s = decode_dss_signature(key.sign(b"statement", ec.ECDSA(hashes.SHA256())))
    n = key.public_key().public_numbers()
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert pc.es256_verify(n.x, n.y, b"statement", raw) and not pc.es256_verify(n.x, n.y, b"statemenT", raw)


def test_the_sample_is_signed_and_its_published_key_matches():
    p = evidence.load(SAMPLE)
    published = json.loads((SAMPLE.parent / "sample-co-signing-key.json").read_text(encoding="utf-8"))
    assert p.signatures == {1: True} and p.manifest_ok
    assert [k["fingerprint_sha256"] for k in p.keys] == [published["fingerprint_sha256"]]
    assert all(e.verification.signature is True for e in p.evidence.values())


def test_a_resealed_forgery_passes_every_hash_and_fails_only_the_signature(tmp_path):
    p = evidence.load(RESEALED)
    assert p.signatures == {1: False}
    assert all(e.verification.content and e.verification.chain and e.verification.inclusion for e in p.evidence.values())
    assert all(e.verification.describe() == "FAILED: batch signature" for e in p.evidence.values())
    # Without signatures the same forgery would pass every check: signing is what catches it.
    with zipfile.ZipFile(RESEALED) as z:
        files = {n: z.read(n) for n in z.namelist()}
    proofs = json.loads(files["proofs.json"])
    for b in proofs["batches"]:
        b["signature"] = b["signature_alg"] = b["signing_key_id"] = None
    proofs["keys"] = []
    files["proofs.json"] = json.dumps(proofs).encode()
    files.pop("manifest.sig.json")
    manifest = json.loads(files["manifest.json"])
    manifest["files"]["proofs.json"] = {"sha256": hashlib.sha256(files["proofs.json"]).hexdigest()}
    files["manifest.json"] = json.dumps(manifest).encode()
    unsigned = tmp_path / "unsigned-resealed.zip"
    with zipfile.ZipFile(unsigned, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    q = evidence.load(unsigned)
    assert q.manifest_ok and q.verified_count == len(q.evidence)  # the forged record now looks genuine
