"""Command line: verify a package, or answer a questionnaire against it.

    python -m vea verify samples/sample-co-evidence-package.zip
    python -m vea answer samples/sample-co-evidence-package.zip samples/questionnaire.csv --provider meta
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vea import evidence, llm, questionnaire
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
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except llm.ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
