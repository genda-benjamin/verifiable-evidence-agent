"""Command line: verify a package, or answer a questionnaire against it.

    python -m vea verify samples/sample-co-evidence-package.zip
    python -m vea answer samples/sample-co-evidence-package.zip samples/questionnaire.csv --provider meta
    python -m vea review out/answers.json
    python -m vea export out/answers.json --out out/proof-pack.zip
    python -m vea check out/proof-pack.zip
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from vea import evaluate, evidence, llm, proofcheck, proofpack, questionnaire
from vea.agent import Agent


def cmd_verify(args) -> int:
    p = evidence.load(args.package)
    bad = [e for e in p.evidence.values() if not e.verification.ok]
    print(f"{p.organization}  {p.period.get('start')} to {p.period.get('end')}")
    print(f"manifest: {'ok' if p.manifest_ok else 'PROBLEMS'}   records: {len(p.evidence)}   verified: {p.verified_count}")
    for m in p.manifest_problems:
        print("  manifest:", m)
    for e in bad:
        print(f"  {e.id}: {e.verification.describe()}  ({e.title})")
    return 0 if p.manifest_ok and not bad else 1


def cmd_answer(args) -> int:
    p = evidence.load(args.package)
    if not p.manifest_ok or p.verified_count != len(p.evidence):
        print("warning: the package has verification failures; failing records cannot be cited.", file=sys.stderr)
    provider = llm.from_env(args.provider)
    verifier = llm.from_env(args.verifier) if args.verifier else provider
    agent = Agent(p, provider, verifier, max_steps=args.max_steps)
    questions = questionnaire.load(args.questionnaire)
    if args.only:
        questions = [q for q in questions if q.id in set(args.only.split(","))]
    results = []
    for q in questions:
        r = agent.answer(q.text)
        results.append({"id": q.id, **r.to_dict()})
        print(f"\n{q.id}  {q.text}\n    -> {r.answer}  [{r.status}]")
        for c in r.claims:
            print(f"       - {c.text}  ({', '.join(i[:8] for i in c.evidence_ids)})")
        if r.caveats:
            print(f"       caveats: {r.caveats}")
        for f in r.flags:
            print(f"       REVIEW: {f}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    usage = {"answering": vars(provider.usage), **({"verifier": vars(verifier.usage)} if verifier is not provider else {})}
    out.write_text(json.dumps({"package": str(args.package), "organization": p.organization, "usage": usage,
                               "results": results}, indent=2, default=str), encoding="utf-8")
    print(f"\nmodel calls: {provider.usage.calls}, tokens in/out: {provider.usage.prompt_tokens}/{provider.usage.completion_tokens}")
    print(f"wrote {out}")
    return 0


def cmd_review(args) -> int:
    """Approve or reject each unreviewed answer in the terminal. The web UI does the same with more context."""
    path = Path(args.answers)
    doc = json.loads(path.read_text(encoding="utf-8"))
    pending = [r for r in doc["results"] if r.get("status") not in ("approved", "rejected")]
    print(f"{len(pending)} answer(s) to review. a = approve, r = reject, s = skip, q = save and quit.")
    for r in pending:
        print(f"\n{r['id']}  {r['question']}\n    -> {r['answer']}")
        for c in r.get("claims") or []:
            print(f"       - {c['text']}  ({', '.join(i[:8] for i in c['evidence_ids'])})")
        if r.get("caveats"):
            print(f"       caveats: {r['caveats']}")
        for f in r.get("flags") or []:
            print(f"       REVIEW: {f}")
        choice = input("    [a/r/s/q] ").strip().lower()[:1]
        if choice == "q":
            break
        if choice in ("a", "r"):
            r["status"] = "approved" if choice == "a" else "rejected"
            r["reviewer_note"] = input("    note (optional): ").strip()
    path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    left = sum(r.get("status") not in ("approved", "rejected") for r in doc["results"])
    print(f"saved {path}; {left} still unreviewed")
    return 0


def cmd_export(args) -> int:
    doc = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    package = evidence.load(args.package or doc["package"])
    try:
        pack = proofpack.build(package, doc["results"], args.out, allow_drafts=args.allow_drafts)
    except proofpack.PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.out}: {len(pack['questions'])} answers, {len(pack['evidence'])} cited records")
    report = proofcheck.verify_pack(args.out)
    print("self-check:", "VERIFIED" if report["ok"] else "NOT VERIFIED")
    for problem in report["problems"]:
        print("  ", problem)
    return 0 if report["ok"] else 1


def cmd_check(args) -> int:
    return proofcheck.main([args.pack])


def cmd_eval(args) -> int:
    doc = evaluate.load_items(args.evalset)
    package = evidence.load(args.package or doc["package"])
    if args.mode == "retrieval":  # no model needed
        r = evaluate.retrieval_recall(package, doc)
        for k, v in r.items():
            if k.startswith("recall_at_"):
                print(f"{k.replace('_', ' ').replace('at ', '@')}: {v['found']}/{r['groups']} ({v['pct']}%)", *v["missed"], sep="\n    ")
        return 0
    provider = llm.from_env(args.provider)
    verifier = llm.from_env(args.verifier) if args.verifier else None
    only = set(args.only.split(",")) if args.only else None
    report = evaluate.run(package, doc, args.mode, provider, verifier, only, progress=print)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out_dir) / f"{stamp}-{args.provider}-{args.mode}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = evaluate.markdown(report)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    print("\n" + md + f"\nwrote {out}.json and .md")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vea", description="Verifiable Evidence Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify an evidence package")
    v.add_argument("package")
    v.set_defaults(fn=cmd_verify)
    a = sub.add_parser("answer", help="answer a questionnaire with cited, verified evidence")
    a.add_argument("package")
    a.add_argument("questionnaire")
    a.add_argument("--provider", default="meta", choices=sorted(llm.PRESETS))
    a.add_argument("--verifier", choices=sorted(llm.PRESETS), help="a different provider for the verifier pass")
    a.add_argument("--max-steps", type=int, default=8)
    a.add_argument("--only", help="comma-separated question ids")
    a.add_argument("--out", default="out/answers.json")
    a.set_defaults(fn=cmd_answer)
    r = sub.add_parser("review", help="approve or reject drafted answers")
    r.add_argument("answers")
    r.set_defaults(fn=cmd_review)
    e = sub.add_parser("export", help="build a proof pack the customer can verify offline")
    e.add_argument("answers")
    e.add_argument("--package", help="the evidence package (default: the one recorded in the answers file)")
    e.add_argument("--out", default="out/proof-pack.zip")
    e.add_argument("--allow-drafts", action="store_true", help="include unreviewed answers, marked DRAFT")
    e.set_defaults(fn=cmd_export)
    c = sub.add_parser("check", help="verify a proof pack (the same check as its verify_pack.py)")
    c.add_argument("pack")
    c.set_defaults(fn=cmd_check)
    ev = sub.add_parser("eval", help="score an answering mode against a labelled question set")
    ev.add_argument("evalset", nargs="?", default="evals/sample-co.json")
    ev.add_argument("--package", help="the evidence package (default: the one named in the eval set)")
    ev.add_argument("--mode", default="agent", choices=("agent", "no-verify", "rag", "retrieval"))
    ev.add_argument("--provider", default="meta", choices=sorted(llm.PRESETS))
    ev.add_argument("--verifier", choices=sorted(llm.PRESETS), help="a different provider for the verifier pass")
    ev.add_argument("--only", help="comma-separated item ids")
    ev.add_argument("--out-dir", default="evals/results")
    ev.set_defaults(fn=cmd_eval)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except llm.ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
