import json
from pathlib import Path

import pytest

from vea import evaluate, evidence
from vea.llm import ScriptedProvider

ROOT = Path(__file__).resolve().parent.parent
EVALSET = ROOT / "evals" / "sample-co.json"
ALL_SETS = sorted((ROOT / "evals").glob("*.json"))


@pytest.fixture(scope="module")
def package():
    return evidence.load(ROOT / "samples" / "sample-co-evidence-package.zip")


@pytest.fixture(scope="module")
def evalset():
    return evaluate.load_items(EVALSET)


def _latest_results(package, matcher):
    kind, _, value = matcher.partition(":")
    ids = set(package.latest.values())
    return [package.get(i).result for i in ids if evaluate.matches(package, i, matcher)]


@pytest.mark.parametrize("path", ALL_SETS, ids=lambda p: p.name)
def test_labels_agree_with_the_evidence(path):
    """The expected answers are checked against each set's own package, so a labelling mistake fails the build."""
    evalset = evaluate.load_items(path)
    package = evidence.load(ROOT / evalset["package"])
    assert len({i["id"] for i in evalset["items"]}) == len(evalset["items"])
    for item in evalset["items"]:
        if item["category"] == "tampered":
            forged = [package.get(i) for i in set(package.latest.values()) if evaluate.matches(package, i, item["forged"])]
            assert forged and all(not e.verification.ok for e in forged), f"{item['id']}: the forged record must fail verification"
            assert "Insufficient evidence" in item["accept"] or item["must_cite"], item["id"]
            for group in item["must_cite"]:  # what an honest answer cites must be intact
                assert all(package.get(i).verification.ok for i in set(package.latest.values()) for m in group
                           if evaluate.matches(package, i, m)), item["id"]
        for group in item["must_cite"]:
            assert any(_latest_results(package, m) for m in group), f"{item['id']}: nothing in the package matches {group}"
        results = [r for g in item["must_cite"] for m in g for r in _latest_results(package, m)]
        if item["category"] in ("pass", "stale", "human-evidence"):
            assert "Yes" in item["accept"] and all(r == "PASS" for r in results), item["id"]
        if item["category"] == "fail":
            assert "Yes" in item["unsafe"] and "FAIL" in results, item["id"]
        if item["category"] == "negated":
            assert item["accept"] == ["Yes"] and "FAIL" in results, item["id"]
        if item["category"] == "stale":
            keys = {package.get(i).check_key for g in item["must_cite"] for m in g for i in package.evidence if evaluate.matches(package, i, m)}
            assert any(package.get(i).result == "FAIL" for i in package.evidence if package.get(i).check_key in keys), \
                f"{item['id']}: a stale trap needs an older FAIL record"
        if item["category"] == "no-evidence":
            assert item["accept"] == ["Insufficient evidence"] and not item["must_cite"]


def test_eval_set_covers_every_trap_category(evalset):
    cats = {i["category"] for i in evalset["items"]}
    assert {"pass", "fail", "stale", "negated", "human-evidence", "incomplete", "no-evidence", "injection"} <= cats
    assert len(evalset["items"]) >= 30 and len({i["id"] for i in evalset["items"]}) == len(evalset["items"])


def test_scoring_distinguishes_grounded_correct_unsafe_and_abstentions(package, evalset):
    items = {i["id"]: i for i in evalset["items"]}
    root = next(i for i in package.latest.values() if package.get(i).record.get("check_id") == "aws.iam.root_mfa")
    other = next(i for i in package.latest.values() if package.get(i).record.get("check_id") == "aws.kms.key_rotation")
    grounded = evaluate.score_item(package, items["E01"], {"answer": "Yes", "claims": [{"evidence_ids": [root]}]})
    ungrounded = evaluate.score_item(package, items["E01"], {"answer": "Yes", "claims": [{"evidence_ids": [other]}]})
    unsafe = evaluate.score_item(package, items["E16"], {"answer": "Yes", "claims": []})
    abstain = evaluate.score_item(package, items["E28"], {"answer": "Insufficient evidence", "claims": []})
    stopped = evaluate.score_item(package, items["E31"], {"answer": "Insufficient evidence", "claims": [],
                                                          "trace": [{"stage": "draft", "answer": "Yes"}],
                                                          "rejected_claims": [{"citation_problems": ["cites no evidence"]}]})
    assert grounded["grounded"] and not ungrounded["grounded"] and ungrounded["correct"]
    assert unsafe["unsafe"] and not unsafe["correct"]
    assert abstain["correct"] and abstain["abstained"] and abstain["should_abstain"]
    assert stopped["pipeline_abstained"] and stopped["correct"] and stopped["rejection_reasons"] == ["no citation"]
    s = evaluate.summarize([grounded, ungrounded, unsafe, abstain, stopped])
    assert s["unsafe_answers"] == 1 and s["pipeline_abstentions"] == 1 and s["correct_abstention_pct"] == 100.0
    assert s["citation_recall_pct"] == 50.0  # E01 twice: one cites the required check, one does not


def test_run_end_to_end_with_scripted_models(package, evalset, tmp_path):
    """Both the agent and the RAG baseline run through the harness; the RAG baseline's unsafe Yes is counted."""
    rag = ScriptedProvider(lambda m: json.dumps({"answer": "Yes", "claims": [{"text": "x", "evidence_ids": ["made-up"]}]}))
    report = evaluate.run(package, evalset, "rag", rag, only={"E16", "E28"})
    assert report["summary"]["unsafe_answers"] == 2 and report["summary"]["accuracy_pct"] == 0.0

    # The same unsafe draft from the agent is stopped by the citation check and becomes an abstention.
    agent_model = ScriptedProvider(lambda m: json.dumps({"action": "answer", "answer": "Yes",
                                                         "claims": [{"text": "x", "evidence_ids": ["made-up"]}]}))
    report = evaluate.run(package, evalset, "agent", agent_model, only={"E16", "E28"})
    s = report["summary"]
    assert s["unsafe_answers"] == 0 and s["pipeline_abstentions"] == 2 and s["rejection_reasons"] == {"invented id": 2}
    md = evaluate.markdown(report)
    assert "| E28 | Insufficient evidence |" in md and "UNSAFE" not in md


def test_an_interrupted_run_resumes_without_asking_the_model_again(package, evalset, tmp_path):
    """Answers are saved after every question, so a crash or a sleeping computer costs one question, not the run."""
    class Stop(Exception):
        pass

    checkpoint, ids, lines = tmp_path / "run.partial.json", {"E01", "E16", "E28"}, []
    abstain = lambda m: json.dumps({"answer": "Insufficient evidence", "claims": []})  # noqa: E731

    def stop_after_two(line):
        lines.append(line)
        if len(lines) == 2:
            raise Stop

    with pytest.raises(Stop):
        evaluate.run(package, evalset, "rag", ScriptedProvider(abstain), only=ids, progress=stop_after_two, checkpoint=checkpoint)
    assert len(json.loads(checkpoint.read_text(encoding="utf-8"))["results"]) == 2

    again = ScriptedProvider(abstain)
    report = evaluate.run(package, evalset, "rag", again, only=ids, checkpoint=checkpoint)
    assert again.usage.calls == 1 and [r["id"] for r in report["results"]] == sorted(ids)
    assert report["usage"]["answering"]["calls"] == 3  # the interrupted session's calls are still counted
    with pytest.raises(ValueError, match="different run"):  # never mixes in answers from another mode or model
        evaluate.run(package, evalset, "agent", ScriptedProvider(abstain), only=ids, checkpoint=checkpoint)


def test_vsaq_questions_are_verbatim_and_attributed():
    doc = evaluate.load_items(ROOT / "evals" / "vsaq.json")
    assert doc["source"]["license"] == "Apache-2.0" and (ROOT / "third_party" / "vsaq" / "LICENSE").exists()
    assert all(i["source"].startswith("google/vsaq@366d670 questionnaires/") for i in doc["items"])
    assert len(doc["items"]) == 29


def test_a_provider_refusal_is_an_abstention_that_is_never_credited(package, evalset):
    from vea.llm import ProviderError

    def refuse(messages):
        raise ProviderError('meta returned HTTP 400: {"error":{"code":"content_policy_violation"}}')

    for mode in ("agent", "rag"):
        report = evaluate.run(package, evalset, mode, ScriptedProvider(refuse), only={"E01", "E28"})
        s = report["summary"]
        assert s["provider_refusals"] == 2 and s["accuracy_pct"] == 0.0, mode  # E28 expects abstention but is not credited
        assert all("answer it by hand" in r["missing"][0] for r in report["results"])


def test_tampered_package_is_reproducible_and_forges_exactly_five_records(tmp_path):
    import hashlib
    import importlib.util
    committed = hashlib.sha256((ROOT / "samples" / "sample-co-tampered.zip").read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("tamper", ROOT / "evals" / "tamper.py")
    tamper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tamper)
    tamper.TARGET, tamper.RESEALED = tmp_path / "rebuilt.zip", tmp_path / "resealed.zip"
    tamper.build()
    tamper.build_resealed()
    assert hashlib.sha256(tamper.TARGET.read_bytes()).hexdigest() == committed
    resealed_committed = hashlib.sha256((ROOT / "samples" / "sample-co-resealed.zip").read_bytes()).hexdigest()
    assert hashlib.sha256(tamper.RESEALED.read_bytes()).hexdigest() == resealed_committed
    p = evidence.load(tamper.TARGET)
    bad = {e.record.get("check_id") or e.title: e.verification.describe() for e in p.evidence.values() if not e.verification.ok}
    # The forger rebuilt every file hash, but cannot re-sign the manifest; the sealed proofs catch each record.
    assert p.manifest_problems == ["manifest signature invalid: the file list was changed after it was signed"]
    assert len(bad) == 5 and bad["aws.iam.no_user_admin_policies"] == "FAILED: Merkle inclusion"


def test_basic_rag_does_not_see_verification_verdicts(package):
    seen = []
    rag = ScriptedProvider(lambda m: seen.append(m[1]["content"]) or json.dumps({"answer": "Yes", "claims": []}))
    evaluate.rag_answer(package, rag, "Is the root account protected with MFA?")
    assert "verification" not in seen[0] and "Merkle" not in seen[0]
