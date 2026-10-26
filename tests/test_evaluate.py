import json
from pathlib import Path

import pytest

from vea import evaluate, evidence
from vea.llm import ScriptedProvider

ROOT = Path(__file__).resolve().parent.parent
EVALSET = ROOT / "evals" / "sample-co.json"


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


def test_labels_agree_with_the_evidence(package, evalset):
    """The expected answers are checked against the package, so a labelling mistake fails the build."""
    for item in evalset["items"]:
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
