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
