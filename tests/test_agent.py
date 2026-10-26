import json
from pathlib import Path

import pytest

from vea import evidence
from vea.agent import Agent, parse_json, review
from vea.llm import ScriptedProvider

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "sample-co-evidence-package.zip"
ROOT_MFA = "6ae91262-4acd-48a8-a375-01992b267489"
CONSOLE_MFA = "915d6815-1b0b-4139-a371-77e7be5b5740"


@pytest.fixture(scope="module")
def package():
    return evidence.load(SAMPLE)


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
        answer("Partially", [{"text": "The root user has MFA enabled.", "evidence_ids": [ROOT_MFA]},
                             {"text": "One of four console users lacks MFA.", "evidence_ids": [CONSOLE_MFA]}]),
        supported(), supported(),
    ])
    r = Agent(package, p).answer("Is MFA enforced?")
    assert r.status == "draft" and r.answer == "Partially"
    assert [c.accepted for c in r.claims] == [True, True]
    assert r.citations() == sorted([ROOT_MFA, CONSOLE_MFA])
    assert review(r, True).status == "approved"


def test_invented_and_unread_citations_are_rejected_without_a_model(package):
    p = ScriptedProvider([
        tool("search_evidence", query="encryption"),
        answer("Yes", [{"text": "Invented.", "evidence_ids": ["00000000-0000-0000-0000-000000000000"]},
                       {"text": "Real but never shown.", "evidence_ids": [ROOT_MFA]},
                       {"text": "No citation.", "evidence_ids": []}]),
    ])
    r = Agent(package, p).answer("Is data encrypted?")
    assert r.answer == "Insufficient evidence" and r.status == "abstained"
    problems = " ".join(pr for c in r.rejected_claims for pr in c.citation_problems)
    assert "no such evidence record" in problems and "never shown" in problems and "cites no evidence" in problems
    assert len(p.seen) == 2  # the verifier was never asked about claims that failed the citation check


def test_verifier_drops_unsupported_claim(package):
    p = ScriptedProvider([
        tool("get_control", control_id="TE-AC-01"),
        answer("Yes", [{"text": "The root user has MFA enabled.", "evidence_ids": [ROOT_MFA]},
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
