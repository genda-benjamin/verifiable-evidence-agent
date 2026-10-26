import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from vea import evidence, proofcheck, proofpack
from vea.agent import Agent
from vea.llm import ScriptedProvider

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "sample-co-evidence-package.zip"
ROOT_MFA = "6ae91262-4acd-48a8-a375-01992b267489"
CONSOLE_MFA = "915d6815-1b0b-4139-a371-77e7be5b5740"


@pytest.fixture(scope="module")
def package():
    return evidence.load(SAMPLE)


def _open(**args):
    return json.dumps({"action": "tool", "tool": "get_evidence", "args": args})


def _answer(ans, claims):
    return json.dumps({"action": "answer", "answer": ans, "claims": claims, "caveats": ""})


@pytest.fixture()
def results(package):
    """Two answered questions and one abstention, produced by the real agent with a scripted model."""
    ok = json.dumps({"supported": True, "reason": "shown"})
    p = ScriptedProvider([
        _open(evidence_ids=[ROOT_MFA, CONSOLE_MFA]),
        _answer("Partially", [{"text": "Root has MFA.", "evidence_ids": [ROOT_MFA]},
                              {"text": "One console user lacks MFA.", "evidence_ids": [CONSOLE_MFA]}]), ok, ok,
        _open(evidence_id=ROOT_MFA), _answer("Yes", [{"text": "Root has MFA.", "evidence_ids": [ROOT_MFA]}]), ok,
        _answer("Insufficient evidence", []),
    ])
    agent = Agent(package, p)
    out = []
    for qid, q in (("Q1", "Is MFA enforced?"), ("Q2", "Is root protected?"), ("Q3", "Is data stored in the EU?")):
        r = agent.answer(q).to_dict()
        r["id"], r["status"] = qid, "approved"
        out.append(r)
    return out


def _rewrite(src: Path, dst: Path, edit):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, edit(n, zin.read(n)))


def test_pack_contains_only_cited_evidence_and_verifies(package, results, tmp_path):
    out = tmp_path / "pack.zip"
    doc = proofpack.build(package, results, out)
    names = zipfile.ZipFile(out).namelist()
    assert sorted(e["evidence_id"] for e in doc["evidence"]) == sorted([ROOT_MFA, CONSOLE_MFA])
    assert len([n for n in names if n.startswith("evidence/")]) == 4  # two records, two files each
    report = proofcheck.verify_pack(out)
    assert report["ok"], report["problems"]
    assert report["records_verified"] == 2 and report["claims"] == 3 and report["questions"] == 3
    assert "unsigned" in report["signatures"]


def test_shipped_verifier_runs_standalone(package, results, tmp_path):
    out = tmp_path / "pack.zip"
    proofpack.build(package, results, out)
    folder = tmp_path / "unzipped"
    zipfile.ZipFile(out).extractall(folder)
    run = subprocess.run([sys.executable, "-I", str(folder / "verify_pack.py"), str(out)], capture_output=True, text=True)
    assert run.returncode == 0 and "RESULT: VERIFIED" in run.stdout


def test_forged_result_is_caught_even_if_the_manifest_is_rebuilt(package, results, tmp_path):
    out = tmp_path / "pack.zip"
    proofpack.build(package, results, out)
    target = f"evidence/{CONSOLE_MFA}/content.json"
    forged = tmp_path / "forged.zip"
    _rewrite(out, forged, lambda n, b: b.replace(b'"FAIL"', b'"PASS"') if n == target else b)
    report = proofcheck.verify_pack(forged)
    assert not report["ok"]
    assert any("changed after the pack was made" in p for p in report["problems"])
    assert any(CONSOLE_MFA in p and "content hash" in p for p in report["problems"])

    # A forger who also rebuilds manifest.json still fails the content-hash check against the sealed record.
    with zipfile.ZipFile(forged) as z:
        files = {n: z.read(n) for n in z.namelist()}
    manifest = json.loads(files["manifest.json"])
    manifest["files"] = {n: proofcheck.sha256(b) for n, b in files.items() if n != "manifest.json"}
    rebuilt = tmp_path / "rebuilt.zip"
    _rewrite(forged, rebuilt, lambda n, b: json.dumps(manifest).encode() if n == "manifest.json" else b)
    report = proofcheck.verify_pack(rebuilt)
    assert not report["ok"] and all("changed after" not in p for p in report["problems"])
    assert any(CONSOLE_MFA in p and "content hash" in p for p in report["problems"])


def test_forger_who_also_fixes_the_record_hashes_fails_the_merkle_proof(package, results, tmp_path):
    out = tmp_path / "pack.zip"
    proofpack.build(package, results, out)
    base = f"evidence/{CONSOLE_MFA}/"
    with zipfile.ZipFile(out) as z:
        content = z.read(base + "content.json").replace(b'"FAIL"', b'"PASS"')
        record = json.loads(z.read(base + "record.json"))
    record["content_sha256"] = proofcheck.sha256(content)
    record["chain_hash"] = proofcheck.sha256(
        f"{record.get('prev_chain_hash') or 'GENESIS'}|{record['content_sha256']}|{record['id']}|{record['collected_at']}".encode())
    forged = tmp_path / "forged.zip"
    _rewrite(out, forged, lambda n, b: content if n == base + "content.json" else json.dumps(record).encode() if n == base + "record.json" else b)
    report = proofcheck.verify_pack(forged)
    record_checks = [p for p in report["problems"] if p.startswith(CONSOLE_MFA)]
    assert not report["ok"] and record_checks == [f"{CONSOLE_MFA}: Merkle inclusion check FAILED"]


def test_drafts_are_refused_unless_allowed_and_rejected_answers_are_left_out(package, results, tmp_path):
    results[0]["status"] = "draft"
    with pytest.raises(proofpack.PackError, match="not reviewed"):
        proofpack.build(package, results, tmp_path / "a.zip")
    doc = proofpack.build(package, results, tmp_path / "b.zip", allow_drafts=True)
    assert doc["questions"][0]["status"] == "draft"
    results[0]["status"] = "rejected"
    doc = proofpack.build(package, results, tmp_path / "c.zip")
    assert [q["id"] for q in doc["questions"]] == ["Q2", "Q3"] and doc["excluded"]["rejected_answers"] == 1
    assert [e["evidence_id"] for e in doc["evidence"]] == [ROOT_MFA]


def test_text_cannot_break_out_of_the_page(package, results, tmp_path):
    results[0]["claims"][0]["text"] = "</script><script>alert(1)</script>"
    out = tmp_path / "pack.zip"
    proofpack.build(package, results, out)
    html = zipfile.ZipFile(out).read("answers.html").decode()
    assert "</script><script>alert(1)" not in html
