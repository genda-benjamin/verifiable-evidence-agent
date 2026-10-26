import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from vea import evidence, proofcheck, proofpack
from vea.audit import AuditLog
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
def log(tmp_path):
    return AuditLog(tmp_path / "answers.audit.jsonl")


def _approve_all(results, log):
    for r in results:
        log.drafted("agent (scripted)", r)
        r["status"] = "approved"
        log.decided("reviewer@sample-co.example", r, True)


@pytest.fixture()
def results(package, log):
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
        r["id"] = qid
        out.append(r)
    _approve_all(out, log)
    return out


def _rewrite(src: Path, dst: Path, edit):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, edit(n, zin.read(n)))


def test_pack_contains_only_cited_evidence_and_verifies(package, results, log, tmp_path):
    out = tmp_path / "pack.zip"
    doc = proofpack.build(package, results, out, log=log, actor="exporter")
    names = zipfile.ZipFile(out).namelist()
    assert sorted(e["evidence_id"] for e in doc["evidence"]) == sorted([ROOT_MFA, CONSOLE_MFA])
    assert len([n for n in names if n.startswith("evidence/")]) == 4  # two records, two files each
    report = proofcheck.verify_pack(out)
    assert report["ok"], report["problems"]
    assert report["records_verified"] == 2 and report["claims"] == 3 and report["questions"] == 3
    assert "unsigned" in report["signatures"]


def test_shipped_verifier_runs_standalone(package, results, log, tmp_path):
    out = tmp_path / "pack.zip"
    proofpack.build(package, results, out, log=log, actor="exporter")
    folder = tmp_path / "unzipped"
    zipfile.ZipFile(out).extractall(folder)
    run = subprocess.run([sys.executable, "-I", str(folder / "verify_pack.py"), str(out)], capture_output=True, text=True)
    assert run.returncode == 0 and "RESULT: VERIFIED" in run.stdout


def test_forged_result_is_caught_even_if_the_manifest_is_rebuilt(package, results, log, tmp_path):
    out = tmp_path / "pack.zip"
    proofpack.build(package, results, out, log=log, actor="exporter")
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


def test_forger_who_also_fixes_the_record_hashes_fails_the_merkle_proof(package, results, log, tmp_path):
    out = tmp_path / "pack.zip"
    proofpack.build(package, results, out, log=log, actor="exporter")
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


def test_drafts_are_refused_unless_allowed_and_rejected_answers_are_left_out(package, results, log, tmp_path):
    results[0]["status"] = "draft"
    with pytest.raises(proofpack.PackError, match="not reviewed"):
        proofpack.build(package, results, tmp_path / "a.zip", log=log, actor="exporter")
    doc = proofpack.build(package, results, tmp_path / "b.zip", log=log, actor="exporter", allow_drafts=True)
    assert doc["questions"][0]["status"] == "draft"
    results[0]["status"] = "rejected"
    log.decided("reviewer@sample-co.example", results[0], False, "not good enough")
    doc = proofpack.build(package, results, tmp_path / "c.zip", log=log, actor="exporter")
    assert [q["id"] for q in doc["questions"]] == ["Q2", "Q3"] and doc["excluded"]["rejected_answers"] == 1
    assert [e["evidence_id"] for e in doc["evidence"]] == [ROOT_MFA]


def test_text_cannot_break_out_of_the_page(package, results, log, tmp_path):
    results[0]["claims"][0]["text"] = "</script><script>alert(1)</script>"
    log.decided("reviewer@sample-co.example", results[0], True)
    out = tmp_path / "pack.zip"
    proofpack.build(package, results, out, log=log, actor="exporter")
    html = zipfile.ZipFile(out).read("answers.html").decode()
    assert "</script><script>alert(1)" not in html


def test_pack_carries_approvals_bound_to_each_answer(package, results, log, tmp_path):
    out = tmp_path / "pack.zip"
    doc = proofpack.build(package, results, out, log=log, actor="exporter")
    assert all(q["approval"]["actor"] == "reviewer@sample-co.example" for q in doc["questions"])
    assert proofcheck.verify_pack(out)["approvals_verified"] == 3
    entries = log.entries()
    assert [e["action"] for e in entries].count("approved") == 3 and entries[-1]["action"] == "exported"
    assert entries[-1]["details"]["pack_sha256"] == proofcheck.sha256(out.read_bytes()) and log.verify() == []


def test_answer_edited_after_approval_is_refused_and_caught(package, results, log, tmp_path):
    results[0]["claims"][0]["text"] = "Root and every other user have MFA."
    with pytest.raises(proofpack.PackError, match="changed after it was approved"):
        proofpack.build(package, results, tmp_path / "a.zip", log=log, actor="exporter")
    # Someone who edits answers.json inside an exported pack is caught by the recipient's verifier.
    results[0]["claims"][0]["text"] = "Root has MFA."
    out = tmp_path / "pack.zip"
    proofpack.build(package, results, out, log=log, actor="exporter")
    with zipfile.ZipFile(out) as z:
        files = {n: z.read(n) for n in z.namelist()}
    answers = json.loads(files["answers.json"])
    answers["questions"][0]["answer"] = "Yes"
    files["answers.json"] = json.dumps(answers).encode()
    manifest = json.loads(files["manifest.json"])
    manifest["files"] = {n: proofcheck.sha256(b) for n, b in files.items() if n != "manifest.json"}
    files["manifest.json"] = json.dumps(manifest).encode()
    edited = tmp_path / "edited.zip"
    with zipfile.ZipFile(edited, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    report = proofcheck.verify_pack(edited)
    assert not report["ok"] and report["problems"] == ["Q1: answer changed after it was approved"]


def test_status_set_by_hand_without_an_approval_is_refused(package, results, log, tmp_path):
    fresh = AuditLog(tmp_path / "empty.audit.jsonl")
    with pytest.raises(proofpack.PackError, match="no approval"):
        proofpack.build(package, results, tmp_path / "a.zip", log=fresh, actor="exporter")


def test_audit_log_detects_edited_removed_and_reordered_entries(results, log):
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert log.verify() == [] and len(lines) == 6  # drafted + approved for each of three answers

    def rewrite(new_lines):
        log.path.write_text("".join(line + "\n" for line in new_lines), encoding="utf-8")

    rewrite(lines[:2] + lines[3:])  # remove the third entry
    assert any("removed or reordered" in p for p in log.verify())

    edited = json.loads(lines[1])
    edited["actor"] = "someone else"  # rewrite who approved Q1
    rewrite([lines[0], json.dumps(edited)] + lines[2:])
    assert log.verify() == ["entry 2: contents changed after it was written"]

    with pytest.raises(ValueError):
        log.append("  ", "approved", "Q1")
