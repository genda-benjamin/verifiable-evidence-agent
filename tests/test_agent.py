import json
from pathlib import Path

import pytest

from vea import evidence
from vea.agent import Agent, parse_json, review
from vea.llm import ScriptedProvider
from vea.tools import Toolbox

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "sample-co-evidence-package.zip"
ROOT_MFA = "6ae91262-4acd-48a8-a375-01992b267489"
CONSOLE_MFA = "915d6815-1b0b-4139-a371-77e7be5b5740"


@pytest.fixture(scope="module")
def package():
    return evidence.load(SAMPLE)


@pytest.fixture(scope="module")
def changed_check(package):
    """A check that failed on an earlier scan and passes on the latest: (stale FAIL record, latest PASS record)."""
    for key, latest_id in package.latest.items():
        latest = package.get(latest_id)
        older = [e for e in package.evidence.values() if e.check_key == key and e.id != latest_id and e.result == "FAIL"]
        if latest.result == "PASS" and older:
            return older[0], latest
    pytest.fail("sample package has no check that changed from FAIL to PASS")


def tool(name, **args):
    return json.dumps({"action": "tool", "tool": name, "args": args, "why": "test"})


def answer(ans, claims, caveats=""):
    return json.dumps({"action": "answer", "answer": ans, "claims": claims, "caveats": caveats})


def supported(ok=True):
    return json.dumps({"supported": ok, "reason": "test"})


def test_parse_json_handles_prose_and_fences():
    assert parse_json('Sure! ```json\n{"a": {"b": "}"}}\n```') == {"a": {"b": "}"}}
    assert parse_json("no json here") is None
    assert parse_json('{bad} then {"ok": 1}') == {"ok": 1}


def test_cited_answer_passes_all_checks(package):
    p = ScriptedProvider([
        tool("get_control", control_id="TE-AC-01"),
        tool("get_evidence", evidence_ids=[ROOT_MFA, CONSOLE_MFA]),
        answer("Partially", [{"text": "The root user has MFA enabled.", "evidence_ids": [ROOT_MFA]},
                             {"text": "One of four console users lacks MFA.", "evidence_ids": [CONSOLE_MFA]}]),
        supported(), supported(),
    ])
    r = Agent(package, p).answer("Is MFA enforced?")
    assert r.status == "draft" and r.answer == "Partially" and r.flags == []
    assert [c.accepted for c in r.claims] == [True, True]
    assert r.citations() == sorted([ROOT_MFA, CONSOLE_MFA])
    assert review(r, True).status == "approved"


def test_invented_unopened_and_uncited_claims_are_rejected_without_a_model(package):
    p = ScriptedProvider([
        tool("get_control", control_id="TE-AC-01"),  # shows ROOT_MFA's id, but does not open the record
        answer("Yes", [{"text": "Invented.", "evidence_ids": ["00000000-0000-0000-0000-000000000000"]},
                       {"text": "Seen but never opened.", "evidence_ids": [ROOT_MFA]},
                       {"text": "No citation.", "evidence_ids": []}]),
    ])
    r = Agent(package, p).answer("Is MFA enforced?")
    assert r.answer == "Insufficient evidence" and r.status == "abstained"
    problems = " ".join(pr for c in r.rejected_claims for pr in c.citation_problems)
    assert "no such evidence record" in problems and "without being opened" in problems and "cites no evidence" in problems
    assert len(p.seen) == 2  # the verifier was never asked about claims that failed the citation check


def test_stale_evidence_alone_is_rejected_but_may_accompany_the_latest(package, changed_check):
    stale, latest = changed_check
    p = ScriptedProvider([
        tool("get_evidence", evidence_ids=[stale.id, latest.id]),
        answer("Yes", [{"text": "It failed.", "evidence_ids": [stale.id]},
                       {"text": "It failed earlier and passes now.", "evidence_ids": [stale.id, latest.id]}]),
        supported(),
    ])
    r = Agent(package, p).answer(latest.title)
    assert [c.text for c in r.claims] == ["It failed earlier and passes now."]
    assert f"superseded by {latest.id}" in r.rejected_claims[0].citation_problems[0]


def test_opening_a_stale_record_tells_the_model_about_the_newer_one(package, changed_check):
    stale, latest = changed_check
    opened = Toolbox(package).get_evidence(stale.id)
    assert opened["superseded_by"]["evidence_id"] == latest.id and opened["superseded_by"]["result"] == "PASS"


def test_search_offers_only_the_latest_record_of_each_check(package):
    hits = Toolbox(package).search_evidence("encrypted encryption", 20)
    assert hits and all(package.superseded_by(h["evidence_id"]) is None for h in hits)
    assert len({package.get(h["evidence_id"]).check_key for h in hits}) == len(hits)


def test_yes_contradicted_by_failing_evidence_is_flagged(package):
    p = ScriptedProvider([
        tool("get_evidence", evidence_id=CONSOLE_MFA),
        answer("Yes", [{"text": "MFA is checked for console users.", "evidence_ids": [CONSOLE_MFA]}]),
        supported(),
    ])
    r = Agent(package, p).answer("Is MFA enforced for everyone?")
    assert r.status == "draft"
    assert any("currently shows FAIL" in f for f in r.flags)
    assert any("finding(s) are open" in f for f in r.flags)


def test_verifier_drops_unsupported_claim(package):
    p = ScriptedProvider([
        tool("get_evidence", evidence_ids=[ROOT_MFA, CONSOLE_MFA]),
        answer("Partially", [{"text": "The root user has MFA enabled.", "evidence_ids": [ROOT_MFA]},
                             {"text": "Every console user has MFA.", "evidence_ids": [CONSOLE_MFA]}]),
        supported(True), supported(False),
    ])
    r = Agent(package, p).answer("Is MFA enforced?")
    assert [c.text for c in r.claims] == ["The root user has MFA enabled."]
    assert r.rejected_claims[0].supported is False
    assert "removed" in r.caveats


def test_tampered_evidence_cannot_be_cited(package):
    e = package.get(ROOT_MFA)
    original = e.verification.content
    e.verification.content = False  # as if content.json had been edited after sealing
    try:
        p = ScriptedProvider([tool("get_evidence", evidence_id=ROOT_MFA),
                              answer("Yes", [{"text": "Root has MFA.", "evidence_ids": [ROOT_MFA]}])])
        r = Agent(package, p).answer("Root MFA?")
        assert r.status == "abstained" and "FAILED: content hash" in r.rejected_claims[0].citation_problems[0]
    finally:
        e.verification.content = original


def test_step_limit_and_bad_json_abstain(package):
    p = ScriptedProvider(lambda m: "thinking..." if len(m) % 2 else tool("list_controls"))
    r = Agent(package, p, max_steps=3).answer("Anything?")
    assert r.status == "abstained" and r.answer == "Insufficient evidence"


def test_list_gaps_names_controls_without_current_evidence(package):
    gaps = {g["control_id"]: g for g in Toolbox(package).list_gaps()}
    assert set(gaps) == {"TE-BC-03", "TE-VM-03"}
    assert gaps["TE-BC-03"]["status"] == "NOT_TESTED" and "booked" in gaps["TE-VM-03"]["why"]


def test_refusal_names_what_is_missing(package):
    p = ScriptedProvider([
        tool("get_control", control_id="TE-BC-03"),
        json.dumps({"action": "answer", "answer": "Insufficient evidence", "claims": [], "caveats": "",
                    "missing": ["A record of a backup restore test"]}),
    ])
    r = Agent(package, p).answer("Do you regularly test restoring from backups?")
    assert r.status == "abstained" and r.missing[0] == "A record of a backup restore test"
    assert any(m.startswith("TE-BC-03 Backup restoration and DR testing: not tested") for m in r.missing)


def test_every_refusal_names_something(package):
    r = Agent(package, ScriptedProvider([answer("Insufficient evidence", [])])).answer("Are you ISO 27001 certified?")
    assert r.missing == ["No evidence record in this package addresses this question."]
    # When the checks remove every claim, the refusal says so instead of staying silent.
    r = Agent(package, ScriptedProvider([answer("Yes", [{"text": "x", "evidence_ids": ["made-up"]}])])).answer("Anything?")
    assert r.status == "abstained" and "not supported" in r.missing[0]


def test_yes_answers_carry_no_missing_list(package):
    p = ScriptedProvider([tool("get_evidence", evidence_id=ROOT_MFA),
                          json.dumps({"action": "answer", "answer": "Yes", "missing": ["noise"],
                                      "claims": [{"text": "Root has MFA.", "evidence_ids": [ROOT_MFA]}]}), supported()])
    assert Agent(package, p).answer("Root MFA?").missing == []


def test_several_ids_in_the_single_id_argument_still_open_them(package):
    tools = Toolbox(package)
    opened = tools.get_evidence(evidence_id=[ROOT_MFA, CONSOLE_MFA])
    assert [o["evidence_id"] for o in opened] == [ROOT_MFA, CONSOLE_MFA] and tools.opened == {ROOT_MFA, CONSOLE_MFA}
