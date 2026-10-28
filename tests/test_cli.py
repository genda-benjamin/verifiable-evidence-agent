import json
from pathlib import Path

import pytest

from vea import __main__ as cli
from vea import llm
from vea.llm import ScriptedProvider

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = str(ROOT / "samples" / "sample-co-evidence-package.zip")
TAMPERED = str(ROOT / "samples" / "sample-co-tampered.zip")
ROOT_MFA = "6ae91262-4acd-48a8-a375-01992b267489"
CONSOLE_MFA = "915d6815-1b0b-4139-a371-77e7be5b5740"


def _model(monkeypatch, replies):
    provider = ScriptedProvider(list(replies))
    monkeypatch.setattr(llm, "from_env", lambda preset: provider)
    return provider


def _tool(name, **args):
    return json.dumps({"action": "tool", "tool": name, "args": args, "why": "find it"})


def _answer(ans, claims):
    return json.dumps({"action": "answer", "answer": ans, "claims": claims, "caveats": "", "missing": []})


def test_ask_with_steps_shows_the_research_and_the_checks(monkeypatch, capsys):
    _model(monkeypatch, [
        _tool("search_evidence", query="root mfa"), _tool("get_evidence", evidence_id=ROOT_MFA),
        _answer("Yes", [{"text": "The root account has MFA.", "evidence_ids": [ROOT_MFA]}]),
        json.dumps({"supported": True, "reason": "shown"}),
    ])
    assert cli.main(["ask", SAMPLE, "Is the root account protected with MFA?", "--steps"]) == 0
    out = capsys.readouterr().out
    assert '[1] search_evidence "root mfa"   (find it)' in out and "[2] get_evidence 6ae91262" in out
    assert "citation check (code): 1 passed, 0 rejected" in out and "verifier (model): 1 supported" in out
    assert "Answer: Yes" in out and "cites 6ae91262 Root user has MFA enabled" in out


def test_compare_on_the_forged_package_shows_rag_believing_and_the_agent_withholding(monkeypatch, capsys):
    claim = [{"text": "All console users have MFA.", "evidence_ids": [CONSOLE_MFA]}]
    _model(monkeypatch, [
        json.dumps({"answer": "Yes", "claims": claim}),  # basic RAG
        _tool("get_evidence", evidence_id=CONSOLE_MFA), _answer("Yes", claim),  # the agent
    ])
    assert cli.main(["compare", TAMPERED, "Is MFA enforced for every console user?", "--steps"]) == 0
    out = capsys.readouterr().out
    rag, agent = out.split("VERIFIABLE EVIDENCE AGENT")
    assert "(5 fail verification)" in rag and "Answer: Yes" in rag and "this record FAILS verification (content hash)" in rag
    assert "WITHHELD" in agent and "Answer: Insufficient evidence" in agent and "REVIEW: Possible tampering" in agent


def test_bare_asks_with_no_evidence(monkeypatch, capsys):
    provider = _model(monkeypatch, ["Yes, always."])
    assert cli.main(["bare", "Are background checks performed?"]) == 0
    sent = provider.seen[0][0]["content"]
    assert "questionnaire for Sample Co" in sent and sent.endswith("Question: Are background checks performed?")
    assert "Yes, always." in capsys.readouterr().out


def test_eval_resumes_after_a_crash_and_cleans_up(monkeypatch, tmp_path, capsys):
    """A run that dies part way keeps its answers; the same command finishes it without asking those questions again."""
    calls, dropped = [], []

    def flaky(messages):  # the connection drops once, on the fourth question
        calls.append(1)
        if len(calls) == 4 and not dropped:
            dropped.append(1)
            raise RuntimeError("connection reset")
        return json.dumps({"answer": "Insufficient evidence", "claims": []})

    monkeypatch.setattr(llm, "from_env", lambda preset: ScriptedProvider(flaky))
    args = ["eval", "--mode", "rag", "--out-dir", str(tmp_path)]
    with pytest.raises(RuntimeError):
        cli.main(args)
    [partial] = tmp_path.glob(".partial-*.json")
    assert len(json.loads(partial.read_text(encoding="utf-8"))["results"]) == 3

    assert cli.main(args) == 0
    assert "resuming: 3 answer(s) saved" in capsys.readouterr().out
    [report] = tmp_path.glob("*-meta-rag.json")
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert len(doc["results"]) == 32 and doc["usage"]["answering"]["calls"] == 3 + 29  # calls saved before the crash count
    assert not list(tmp_path.glob(".partial-*"))  # finished, so nothing is left to resume

    calls.clear()
    assert cli.main(args + ["--fresh"]) == 0 and len(calls) == 32
