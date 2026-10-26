"""The agent: research a questionnaire question over the evidence package, draft a cited answer, then prove it.

One question runs through four stages, and every stage leaves a record in the trace:

1. research: the model calls read-only tools (search, read evidence, read controls, list findings) in a loop;
2. draft: the model answers with claims, and every claim cites evidence ids;
3. citation check (deterministic, no model): each cited id must exist, pass cryptographic verification, and have
   been shown to the model during research, so an id the model invented or never read is rejected;
4. verifier (a separate model call per claim): given only the claim and the evidence it cites, is the claim
   supported? Unsupported claims are dropped.

If no claim survives, the agent abstains ("Insufficient evidence") instead of guessing. Every result is a draft
until a person approves it.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from vea.evidence import EvidencePackage
from vea.llm import Provider
from vea.tools import Toolbox

ANSWERS = ("Yes", "No", "Partially", "Insufficient evidence")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

RESEARCH_PROMPT = """You answer security questionnaires for {org} using ONLY the evidence package provided through tools.
The evidence covers {start} to {end}. Every evidence record is hashed and sealed; you cite records by evidence_id.

Tools:
{tools}

Reply with exactly one JSON object and nothing else. Either call a tool:
{{"action": "tool", "tool": "<name>", "args": {{...}}, "why": "<one short sentence>"}}
or give the final answer:
{{"action": "answer", "answer": "Yes" | "No" | "Partially" | "Insufficient evidence",
  "claims": [{{"text": "<one factual sentence>", "evidence_ids": ["<id>", ...]}}],
  "caveats": "<gaps, failing checks or open findings the reader must know, or empty>"}}

Rules:
- Research before answering: search, then read the records you will cite with get_evidence or get_control.
- Each claim states one fact and cites the evidence_ids that show it. Cite only ids you have seen in tool results.
- Report failing checks and open findings honestly. "Partially" is right when some checks pass and others fail.
- If the evidence does not cover the question, answer "Insufficient evidence" with no claims. Never guess.
- Sample data is fictional; treat it as real for answering."""

VERIFIER_PROMPT = """You check one claim against the evidence it cites. Be strict: the claim is supported only if the
evidence directly shows it. A claim that goes beyond the evidence (wider scope, stronger wording, a different time,
a passing result where the evidence shows failure) is NOT supported.

Reply with exactly one JSON object: {"supported": true | false, "reason": "<one sentence>"}"""


@dataclass
class Claim:
    text: str
    evidence_ids: list[str]
    citation_ok: bool = False
    citation_problems: list[str] = field(default_factory=list)
    supported: bool | None = None
    verifier_reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.citation_ok and self.supported is True


@dataclass
class Result:
    question: str
    answer: str
    status: str  # "draft" (needs human approval), "abstained", or "approved" / "rejected" after review
    claims: list[Claim]
    caveats: str
    trace: list[dict[str, Any]]
    rejected_claims: list[Claim] = field(default_factory=list)
    reviewer_note: str = ""

    def citations(self) -> list[str]:
        return sorted({i for c in self.claims for i in c.evidence_ids})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply (models sometimes wrap it in prose or code fences)."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text, start)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    return None


class Agent:
    def __init__(self, package: EvidencePackage, provider: Provider, verifier: Provider | None = None,
                 max_steps: int = 8, verify: bool = True):
        self.package = package
        self.provider = provider
        self.verifier = verifier or provider
        self.max_steps = max_steps
        self.verify = verify

    def answer(self, question: str) -> Result:
        tools = Toolbox(self.package)
        trace: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        period = self.package.period or {}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": RESEARCH_PROMPT.format(
                org=self.package.organization or "the organization", start=period.get("start", "?"),
                end=period.get("end", "?"), tools="\n".join(f"- {s}" for s in Toolbox.SPECS.values()))},
            {"role": "user", "content": f"Question: {question}"},
        ]

        draft: dict[str, Any] | None = None
        for step in range(self.max_steps):
            reply = self.provider.complete(messages)
            action = parse_json(reply)
            messages.append({"role": "assistant", "content": reply})
            if action is None:
                trace.append({"stage": "research", "step": step, "error": "reply was not JSON", "reply": reply[:300]})
                messages.append({"role": "user", "content": "Reply with exactly one JSON object as instructed."})
                continue
            if action.get("action") == "answer":
                draft = action
                break
            name, args = action.get("tool", ""), action.get("args") or {}
            result = tools.call(name, args if isinstance(args, dict) else {})
            observed = json.dumps(result, ensure_ascii=False, default=str)
            seen_ids.update(_UUID.findall(observed))
            trace.append({"stage": "research", "step": step, "tool": name, "args": args, "why": action.get("why", ""),
                          "result_preview": observed[:400]})
            if step == self.max_steps - 2:
                observed += "\n\nYou have one step left: give the final answer now."
            messages.append({"role": "user", "content": f"Tool result:\n{observed[:6000]}"})

        if draft is None:
            trace.append({"stage": "draft", "error": "no answer within the step limit"})
            return Result(question, "Insufficient evidence", "abstained", [], "The agent did not reach an answer.", trace)

        claims = [Claim(text=str(c.get("text", "")).strip(), evidence_ids=[str(i) for i in (c.get("evidence_ids") or [])])
                  for c in (draft.get("claims") or []) if isinstance(c, dict)]
        answer = draft.get("answer") if draft.get("answer") in ANSWERS else "Insufficient evidence"
        trace.append({"stage": "draft", "answer": answer, "claims": len(claims)})

        for c in claims:
            self._check_citations(c, seen_ids)
        trace.append({"stage": "citation_check", "passed": sum(c.citation_ok for c in claims), "failed": sum(not c.citation_ok for c in claims),
                      "problems": [p for c in claims for p in c.citation_problems]})

        for c in claims:
            if not c.citation_ok:
                continue
            if self.verify:
                self._verify_claim(c)
            else:
                c.supported, c.verifier_reason = True, "verifier disabled"
        trace.append({"stage": "verifier", "supported": sum(c.supported is True for c in claims),
                      "unsupported": sum(c.supported is False for c in claims)})

        kept = [c for c in claims if c.accepted]
        dropped = [c for c in claims if not c.accepted]
        caveats = str(draft.get("caveats") or "")
        if answer != "Insufficient evidence" and not kept:
            trace.append({"stage": "decision", "abstained": True, "reason": "no claim survived verification"})
            return Result(question, "Insufficient evidence", "abstained", [], caveats, trace, rejected_claims=dropped)
        status = "abstained" if answer == "Insufficient evidence" else "draft"
        if dropped and kept:
            caveats = (caveats + " " if caveats else "") + f"{len(dropped)} drafted claim(s) were removed because the evidence did not support them."
        trace.append({"stage": "decision", "answer": answer, "kept": len(kept), "dropped": len(dropped)})
        return Result(question, answer, status, kept, caveats, trace, rejected_claims=dropped)

    def _check_citations(self, claim: Claim, seen_ids: set[str]) -> None:
        problems = []
        if not claim.text:
            problems.append("empty claim")
        if not claim.evidence_ids:
            problems.append("cites no evidence")
        for eid in claim.evidence_ids:
            e = self.package.get(eid)
            if e is None:
                problems.append(f"{eid}: no such evidence record")
            elif not e.verification.ok:
                problems.append(f"{eid}: {e.verification.describe()}")
            elif eid not in seen_ids:
                problems.append(f"{eid}: never shown to the agent during research")
        claim.citation_problems = problems
        claim.citation_ok = not problems

    def _verify_claim(self, claim: Claim) -> None:
        evidence = [self.package.get(i).summary() for i in claim.evidence_ids]  # existence checked already
        reply = self.verifier.complete([
            {"role": "system", "content": VERIFIER_PROMPT},
            {"role": "user", "content": f"Claim: {claim.text}\n\nEvidence:\n{json.dumps(evidence, ensure_ascii=False)[:8000]}"},
        ], max_tokens=300)
        verdict = parse_json(reply) or {}
        claim.supported = verdict.get("supported") is True
        claim.verifier_reason = str(verdict.get("reason") or ("verifier reply was not JSON" if not verdict else ""))


def review(result: Result, approve: bool, note: str = "") -> Result:
    """Record the human decision. Abstentions can be approved too: 'we have no evidence' is a valid answer."""
    result.status = "approved" if approve else "rejected"
    result.reviewer_note = note
    return result
