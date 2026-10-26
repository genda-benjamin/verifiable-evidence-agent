"""Evaluate answering modes against a labelled question set and report what each layer of the pipeline adds.

Modes, all on the same model:

* agent:      the full pipeline (research tools, citation check, verifier, abstention, contradiction flags);
* no-verify:  the same without the verifier model pass (ablation: what does the verifier add?);
* rag:        "basic RAG": retrieve the top evidence, ask once, take the answer as given. No checks.

Metrics (see evals/README.md for definitions): answer accuracy, unsafe answer rate, grounded accuracy (right answer
AND the required evidence cited), abstention behaviour, and where drafted claims were stopped.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from vea.agent import ANSWERS, Agent, parse_json
from vea.evidence import EvidencePackage
from vea.llm import Provider
from vea.tools import Toolbox

ABSTAIN = "Insufficient evidence"


def load_items(path: str | Path) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    for item in doc["items"]:
        bad = [a for a in item["accept"] + item.get("unsafe", []) if a not in ANSWERS]
        if bad:
            raise ValueError(f"{item['id']}: unknown answer label(s) {bad}")
    return doc


def matches(package: EvidencePackage, evidence_id: str, matcher: str) -> bool:
    e = package.get(evidence_id)
    if e is None:
        return False
    kind, _, value = matcher.partition(":")
    if kind == "check":
        return e.record.get("check_id") == value
    if kind == "control":
        return value in e.control_ids
    if kind == "review":  # a person's review of the control; an uploaded policy document is not proof of operation
        return e.record.get("source_type") == "human_review" and value in e.control_ids
    raise ValueError(f"unknown matcher {matcher}")


def score_item(package: EvidencePackage, item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    answer = result.get("answer")
    cited = [i for c in result.get("claims") or [] for i in c.get("evidence_ids") or []]
    groups = item.get("must_cite") or []
    covered = [any(matches(package, i, m) for i in cited for m in g) for g in groups]
    correct = answer in item["accept"]
    answered = answer != ABSTAIN
    drafted = next((t.get("answer") for t in result.get("trace") or [] if t.get("stage") == "draft"), answer)
    return {
        "id": item["id"], "category": item["category"], "answer": answer, "accept": item["accept"],
        "correct": correct,
        "unsafe": answer in item.get("unsafe", []),
        "grounded": correct and (not answered or all(covered)),
        "citation_groups": len(groups), "citation_groups_covered": sum(covered),
        "should_abstain": item["accept"] == [ABSTAIN],
        "abstained": not answered,
        "pipeline_abstained": drafted not in (None, ABSTAIN) and not answered,  # the model answered; the checks stopped it
        "claims_kept": len(result.get("claims") or []),
        "claims_rejected": len(result.get("rejected_claims") or []),
        "rejection_reasons": [_reason(c) for c in result.get("rejected_claims") or []],
        "flags": len(result.get("flags") or []),
    }


def _reason(claim: dict[str, Any]) -> str:
    problems = " ".join(claim.get("citation_problems") or [])
    for key, label in (("no such evidence", "invented id"), ("FAILED", "failed verification"), ("without being opened", "not opened"),
                       ("stale", "stale record"), ("cites no evidence", "no citation"), ("empty claim", "empty claim")):
        if key in problems:
            return label
    return "verifier: unsupported" if claim.get("supported") is False else "other"


def summarize(scored: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(scored) or 1
    answerable = [s for s in scored if not s["should_abstain"]]
    unanswerable = [s for s in scored if s["should_abstain"]]
    pct = lambda k, xs: round(100 * sum(1 for s in xs if s[k]) / len(xs), 1) if xs else None
    groups = sum(s["citation_groups"] for s in scored if s["correct"] and not s["abstained"])
    covered = sum(s["citation_groups_covered"] for s in scored if s["correct"] and not s["abstained"])
    by_cat: dict[str, dict[str, Any]] = {}
    for cat in sorted({s["category"] for s in scored}):
        xs = [s for s in scored if s["category"] == cat]
        by_cat[cat] = {"n": len(xs), "accuracy": pct("correct", xs), "grounded": pct("grounded", xs), "unsafe": sum(s["unsafe"] for s in xs)}
    return {
        "questions": len(scored),
        "accuracy_pct": pct("correct", scored),
        "grounded_accuracy_pct": pct("grounded", scored),
        "unsafe_answers": sum(s["unsafe"] for s in scored),
        "unsafe_pct": round(100 * sum(s["unsafe"] for s in scored) / n, 1),
        "citation_recall_pct": round(100 * covered / groups, 1) if groups else None,
        "correct_abstention_pct": pct("abstained", unanswerable),
        "false_abstention_pct": pct("abstained", answerable),
        "pipeline_abstentions": sum(s["pipeline_abstained"] for s in scored),
        "claims_kept": sum(s["claims_kept"] for s in scored),
        "claims_rejected": sum(s["claims_rejected"] for s in scored),
        "rejection_reasons": dict(Counter(r for s in scored for r in s["rejection_reasons"])),
        "answers_flagged": sum(1 for s in scored if s["flags"]),
        "by_category": by_cat,
    }


def retrieval_recall(package: EvidencePackage, eval_doc: dict[str, Any], ks: tuple[int, ...] = (1, 3, 8)) -> dict[str, Any]:
    """Model-free: how often does one search with the question's own text surface the required evidence?

    The agent may search several times with its own queries, so this is a floor for what the agent can find."""
    tools = Toolbox(package)
    groups = [(item["id"], g) for item in eval_doc["items"] for g in item.get("must_cite") or []]
    out: dict[str, Any] = {"groups": len(groups)}
    for k in ks:
        hits = {item["id"]: [h["evidence_id"] for h in tools.search_evidence(item["question"], k)] for item in eval_doc["items"]}
        missed = [f"{qid}: {' or '.join(g)}" for qid, g in groups if not any(matches(package, i, m) for i in hits[qid] for m in g)]
        out[f"recall_at_{k}"] = {"found": len(groups) - len(missed), "pct": round(100 * (len(groups) - len(missed)) / max(1, len(groups)), 1),
                                 "missed": missed}
    return out


RAG_PROMPT = """You answer security questionnaire questions for {org} using the evidence records below.
Reply with exactly one JSON object:
{{"answer": "Yes" | "No" | "Partially" | "Insufficient evidence",
  "claims": [{{"text": "<one factual sentence>", "evidence_ids": ["<id>", ...]}}]}}"""


def rag_answer(package: EvidencePackage, provider: Provider, question: str, k: int = 8) -> dict[str, Any]:
    """Basic retrieval-augmented generation: top-k search hits in the prompt, one model call, no checks."""
    tools = Toolbox(package)
    hits = tools.search_evidence(question, k)
    context = [package.get(h["evidence_id"]).summary() for h in hits]
    reply = provider.complete([
        {"role": "system", "content": RAG_PROMPT.format(org=package.organization or "the company")},
        {"role": "user", "content": f"Evidence:\n{json.dumps(context, ensure_ascii=False)[:12000]}\n\nQuestion: {question}"},
    ])
    out = parse_json(reply) or {}
    answer = out.get("answer") if out.get("answer") in ANSWERS else ABSTAIN
    claims = [{"text": str(c.get("text", "")), "evidence_ids": [str(i) for i in c.get("evidence_ids") or []]}
              for c in out.get("claims") or [] if isinstance(c, dict)]
    return {"question": question, "answer": answer, "claims": claims, "rejected_claims": [], "flags": [],
            "trace": [{"stage": "draft", "answer": answer}]}


def run(package: EvidencePackage, eval_doc: dict[str, Any], mode: str, provider: Provider, verifier: Provider | None = None,
        only: set[str] | None = None, progress: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    if mode not in ("agent", "no-verify", "rag"):
        raise ValueError(f"unknown mode {mode}")
    agent = Agent(package, provider, verifier, verify=(mode == "agent"))
    scored, raw = [], []
    started = time.monotonic()
    for item in eval_doc["items"]:
        if only and item["id"] not in only:
            continue
        result = rag_answer(package, provider, item["question"]) if mode == "rag" else agent.answer(item["question"]).to_dict()
        s = score_item(package, item, result)
        scored.append(s)
        raw.append({"id": item["id"], **result})
        progress(f"{item['id']} {'ok  ' if s['correct'] else 'MISS'}{' UNSAFE' if s['unsafe'] else ''}  {s['answer']}")
    usage = {"answering": vars(provider.usage)}
    if verifier is not None and verifier is not provider:
        usage["verifier"] = vars(verifier.usage)
    return {"mode": mode, "provider": getattr(provider, "name", "?"), "model": getattr(provider, "model", "?"),
            "verifier": getattr(verifier, "model", None) if verifier is not None and verifier is not provider else None,
            "seconds": round(time.monotonic() - started, 1), "usage": usage,
            "summary": summarize(scored), "scored": scored, "results": raw}


def markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    rows = [("Questions", s["questions"]), ("Answer accuracy", f"{s['accuracy_pct']}%"),
            ("Grounded accuracy (right answer and required evidence cited)", f"{s['grounded_accuracy_pct']}%"),
            ("Unsafe answers (would mislead a customer)", f"{s['unsafe_answers']} ({s['unsafe_pct']}%)"),
            ("Citation recall on correct answers", f"{s['citation_recall_pct']}%"),
            ("Correct abstentions (no evidence exists)", f"{s['correct_abstention_pct']}%"),
            ("False abstentions (evidence exists)", f"{s['false_abstention_pct']}%"),
            ("Answers the checks turned into abstentions", s["pipeline_abstentions"]),
            ("Claims kept / rejected", f"{s['claims_kept']} / {s['claims_rejected']}"),
            ("Rejection reasons", ", ".join(f"{k}: {v}" for k, v in s["rejection_reasons"].items()) or "none"),
            ("Answers flagged for the reviewer", s["answers_flagged"])]
    u = report["usage"]["answering"]
    lines = [f"# Evaluation: {report['mode']} on {report['model']}", "",
             f"Provider `{report['provider']}`" + (f", verifier `{report['verifier']}`" if report.get("verifier") else "") +
             f". {report['seconds']} s, {u['calls']} model calls, {u['prompt_tokens']} prompt / {u['completion_tokens']} completion tokens.", "",
             "| Metric | Value |", "|---|---|", *[f"| {k} | {v} |" for k, v in rows], "",
             "| Category | n | Accuracy | Grounded | Unsafe |", "|---|---|---|---|---|",
             *[f"| {c} | {v['n']} | {v['accuracy']}% | {v['grounded']}% | {v['unsafe']} |" for c, v in s["by_category"].items()], "",
             "| Id | Answer | Expected | Result |", "|---|---|---|---|",
             *[f"| {x['id']} | {x['answer']} | {' / '.join(x['accept'])} | {'UNSAFE' if x['unsafe'] else 'grounded' if x['grounded'] else 'correct, citation missing' if x['correct'] else 'wrong'} |"
               for x in report["scored"]]]
    return "\n".join(lines) + "\n"
