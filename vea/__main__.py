"""Command line: verify a package, or answer a questionnaire against it.

    python -m vea verify samples/sample-co-evidence-package.zip
    python -m vea answer samples/sample-co-evidence-package.zip samples/questionnaire.csv --provider meta
    python -m vea review out/answers.json --reviewer "Jane Doe <jane@example.com>"
    python -m vea export out/answers.json --by "Jane Doe <jane@example.com>" --out out/proof-pack.zip
    python -m vea check out/proof-pack.zip
    python -m vea ask samples/sample-co-evidence-package.zip "Is MFA enforced?" --steps
    python -m vea compare samples/sample-co-tampered.zip "Is MFA enforced for every console user?"
    python -m vea bare "Are background checks performed on personnel?"
    python -m vea intake samples/questionnaire.pdf --out out/questions.csv
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import textwrap
import time
from pathlib import Path

from vea import evaluate, evidence, intake, llm, proofcheck, proofpack, questionnaire
from vea.audit import AuditLog
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


def wrap(text: str, first: str, rest: str | None = None) -> None:
    """Print wrapped at word boundaries to the terminal's width, continuation lines indented under the text."""
    width = max(40, min(shutil.get_terminal_size((100, 20)).columns, 160) - 1)
    print(textwrap.fill(text, width=width, initial_indent=first, subsequent_indent=" " * len(first) if rest is None else rest))


def _short(package, eid: str) -> str:
    e = package.get(eid)
    return f"{eid[:8]} {e.title}" if e else f"{eid[:8]} (no such record)"


def step_printer(package):
    """The --steps view: print each research step and check as the agent takes it."""
    n = [0]

    def show(entry: dict) -> None:
        stage = entry.get("stage")
        if stage == "research":
            n[0] += 1
            if entry.get("provider_error"):
                print(f"  [{n[0]}] the model provider refused this question")
            elif entry.get("error"):
                print(f"  [{n[0]}] unreadable reply; asked again")
            else:
                a = entry.get("args") or {}
                ids = a.get("evidence_ids") or a.get("evidence_id") or []
                ids = ids if isinstance(ids, list) else [ids]
                detail = (f'"{a.get("query", "")}"' if entry["tool"] == "search_evidence" else
                          ", ".join(i[:8] for i in ids) if entry["tool"] == "get_evidence" else
                          a.get("control_id", "") if entry["tool"] == "get_control" else a.get("status", ""))
                why = f"   ({entry['why']})" if entry.get("why") else ""
                wrap(f"{entry['tool']} {detail}{why}", f"  [{n[0]}] ")
        elif stage == "draft" and "answer" in entry:
            print(f"  draft: {entry['answer']}, {entry['claims']} claim(s)")
        elif stage == "citation_check" and entry["passed"] + entry["failed"]:
            print(f"  citation check (code): {entry['passed']} passed, {entry['failed']} rejected")
            for problem in entry.get("problems") or []:
                wrap(problem, "      rejected: ")
        elif stage == "verifier" and entry["supported"] + entry["unsupported"]:
            print(f"  verifier (model): {entry['supported']} supported, {entry['unsupported']} not supported")
        elif stage == "decision" and entry.get("tampered"):
            print("  WITHHELD: the draft relied on evidence that fails cryptographic verification")
    return show


def print_result(r, package, indent: str = "  ") -> None:
    print(f"{indent}Answer: {r.answer}")
    for c in r.claims:
        wrap(c.text, f"{indent}  - ")
        for i in c.evidence_ids:
            wrap(f"cites {_short(package, i)}", f"{indent}      ")
    if r.caveats:
        wrap(r.caveats, f"{indent}Caveats: ")
    for m in r.missing:
        wrap(m, f"{indent}Missing: ")
    for f in r.flags:
        wrap(f, f"{indent}REVIEW: ")


def _agent(args, package) -> Agent:
    provider = llm.from_env(args.provider)
    verifier = llm.from_env(args.verifier) if getattr(args, "verifier", None) else provider
    return Agent(package, provider, verifier, max_steps=getattr(args, "max_steps", 8),
                 on_step=step_printer(package) if getattr(args, "steps", False) else None)


def cmd_ask(args) -> int:
    """One question, answered by the agent, optionally showing every step."""
    p = evidence.load(args.package)
    agent = _agent(args, p)
    print(f"Question: {args.question}\n")
    r = agent.answer(args.question)
    print()
    print_result(r, p)
    u = agent.provider.usage
    print(f"\n({u.calls} model calls, {u.prompt_tokens + u.completion_tokens} tokens, {u.seconds:.0f} s)")
    return 0


def cmd_compare(args) -> int:
    """The same question, answered by basic RAG and then by the agent, on the same evidence."""
    p = evidence.load(args.package)
    bad = sum(1 for e in p.evidence.values() if not e.verification.ok)
    print(f"Evidence package: {p.organization}, {len(p.evidence)} records" + (f" ({bad} fail verification)" if bad else ""))
    print(f"Question: {args.question}\n")
    print("BASIC RAG (one search, one model call, no checks)")
    rag = evaluate.rag_answer(p, llm.from_env(args.provider), args.question)
    print(f"  Answer: {rag['answer']}")
    for c in rag["claims"]:
        wrap(c["text"], "    - ")
        for i in c["evidence_ids"]:
            e = p.get(i)
            note = "" if e is None or e.verification.ok else f"   <- this record FAILS verification ({e.verification.describe()[8:]})"
            print(f"        cites {_short(p, i)}{note}")
    print("\nVERIFIABLE EVIDENCE AGENT")
    r = _agent(args, p).answer(args.question)
    print()
    print_result(r, p)
    return 0


def cmd_intake(args) -> int:
    """Read a questionnaire PDF or screenshot with a vision model into a CSV for a person to check."""
    provider = llm.from_env(args.provider)
    try:
        questions = intake.extract(args.file, provider)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for q in questions:
        wrap(q.text, f"{q.id}  ")
        print(f"      [{q.check}]")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    intake.write_csv(questions, out)
    flagged = [q for q in questions if q.check != "verbatim"]
    pages = max((q.page for q in questions), default=0)
    print(f"\n{len(questions)} questions from {pages} page(s); {len(questions) - len(flagged)} match the document's text "
          f"word for word, {len(flagged)} to check by hand.")
    print(f"wrote {out}. Check it against the original and correct it, then answer it:\n"
          f"  python -m vea answer <evidence package> {out}")
    return 0


def cmd_bare(args) -> int:
    """Ask the model to fill in a questionnaire answer with no evidence, the way people use chat assistants for this:
    what an unchecked answer looks like."""
    provider = llm.from_env(args.provider)
    prompt = (f"You are filling in a customer's security questionnaire for {args.company}. Answer Yes, No or Partially, "
              f"with a one-sentence explanation.\n\nQuestion: {args.question}")
    print(f"{args.company}, security questionnaire (no evidence given)\nQuestion: {args.question}\n")
    print(provider.complete([{"role": "user", "content": prompt}], max_tokens=4000).strip())
    return 0


def cmd_answer(args) -> int:
    p = evidence.load(args.package)
    if not p.manifest_ok or p.verified_count != len(p.evidence):
        print("warning: the package has verification failures; failing records cannot be cited.", file=sys.stderr)
    agent = _agent(args, p)
    provider, verifier = agent.provider, agent.verifier
    questions = questionnaire.load(args.questionnaire)
    if args.only:
        questions = [q for q in questions if q.id in set(args.only.split(","))]
    out = Path(args.out)
    log = AuditLog(out.with_suffix(".audit.jsonl"))
    results = []
    for q in questions:
        r = agent.answer(q.text)
        results.append({"id": q.id, **r.to_dict()})
        log.drafted(f"agent ({provider.name}: {provider.model})", results[-1])
        print(f"\n{q.id}  {q.text}\n    -> {r.answer}  [{r.status}]")
        for c in r.claims:
            print(f"       - {c.text}  ({', '.join(i[:8] for i in c.evidence_ids)})")
        if r.caveats:
            print(f"       caveats: {r.caveats}")
        for m in r.missing:
            print(f"       missing: {m}")
        for f in r.flags:
            print(f"       REVIEW: {f}")
    out.parent.mkdir(parents=True, exist_ok=True)
    usage = {"answering": vars(provider.usage), **({"verifier": vars(verifier.usage)} if verifier is not provider else {})}
    out.write_text(json.dumps({"package": str(args.package), "organization": p.organization, "usage": usage,
                               "audit_log": str(log.path), "results": results}, indent=2, default=str), encoding="utf-8")
    print(f"\nmodel calls: {provider.usage.calls}, tokens in/out: {provider.usage.prompt_tokens}/{provider.usage.completion_tokens}")
    print(f"wrote {out}")
    return 0


def cmd_review(args) -> int:
    """Approve or reject each unreviewed answer in the terminal. The web UI does the same with more context."""
    path = Path(args.answers)
    doc = json.loads(path.read_text(encoding="utf-8"))
    log = AuditLog(doc.get("audit_log") or path.with_suffix(".audit.jsonl"))
    pending = [r for r in doc["results"] if r.get("status") not in ("approved", "rejected")]
    width = max(40, min(shutil.get_terminal_size((100, 20)).columns, 120) - 1)

    def say(text: str, first: str, rest: str = "") -> None:  # wrapped to the terminal so long lines stay readable
        print(textwrap.fill(text, width=width, initial_indent=first, subsequent_indent=rest or " " * len(first)))

    print(f"{len(pending)} answer(s) to review. a = approve, r = reject, s = skip, q = save and quit.")
    for r in pending:
        print()
        say(r["question"], f"{r['id']}  ")
        say(r["answer"], "    Answer: ")
        for c in r.get("claims") or []:
            say(f"{c['text']}  ({', '.join(i[:8] for i in c['evidence_ids'])})", "      - ", "        ")
        if r.get("caveats"):
            say(r["caveats"], "    Caveats: ", "      ")
        for m in r.get("missing") or []:
            say(m, "    Missing: ", "      ")
        for f in r.get("flags") or []:
            say(f, "    REVIEW: ", "      ")
        choice = input("    [a/r/s/q] ").strip().lower()[:1]
        if choice == "q":
            break
        if choice in ("a", "r"):
            r["status"] = "approved" if choice == "a" else "rejected"
            r["reviewer_note"] = input("    note (optional): ").strip()
            log.decided(args.reviewer, r, choice == "a", r["reviewer_note"])
    path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    left = sum(r.get("status") not in ("approved", "rejected") for r in doc["results"])
    print(f"saved {path}; {left} still unreviewed")
    return 0


def cmd_export(args) -> int:
    doc = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    package = evidence.load(args.package or doc["package"])
    log = AuditLog(doc.get("audit_log") or Path(args.answers).with_suffix(".audit.jsonl"))
    try:
        pack = proofpack.build(package, doc["results"], args.out, log=log, actor=args.by, allow_drafts=args.allow_drafts)
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
    return proofcheck.main([args.pack] + (["--key", args.key] if args.key else []))


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
    who = args.provider + (f"+{args.verifier}-verifier" if args.verifier and args.verifier != args.provider else "")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Full runs save every answer as they go; running the same command again after an interruption resumes it.
    checkpoint = None if only else out_dir / f".partial-{Path(args.evalset).stem}-{who}-{args.mode}.json"
    if checkpoint is not None and args.fresh:
        checkpoint.unlink(missing_ok=True)
    report = evaluate.run(package, doc, args.mode, provider, verifier, only, progress=print, checkpoint=checkpoint)
    out = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{who}-{args.mode}"
    out.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = evaluate.markdown(report)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    if checkpoint is not None:
        checkpoint.unlink(missing_ok=True)
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
    a.add_argument("--steps", action="store_true", help="show each research step and check as it happens")
    a.set_defaults(fn=cmd_answer)
    for name, fn, help_text in (("ask", cmd_ask, "answer one question with the agent"),
                                ("compare", cmd_compare, "answer one question with basic RAG, then with the agent")):
        q = sub.add_parser(name, help=help_text)
        q.add_argument("package")
        q.add_argument("question")
        q.add_argument("--provider", default="meta", choices=sorted(llm.PRESETS))
        q.add_argument("--verifier", choices=sorted(llm.PRESETS), help="a different provider for the verifier pass")
        q.add_argument("--steps", action="store_true", help="show each research step and check as it happens")
        q.set_defaults(fn=fn)
    it = sub.add_parser("intake", help="read a questionnaire PDF or screenshot into a CSV, checked against its text")
    it.add_argument("file")
    it.add_argument("--provider", default="meta", choices=sorted(llm.PRESETS))
    it.add_argument("--out", default="out/questions.csv")
    it.set_defaults(fn=cmd_intake)
    b = sub.add_parser("bare", help="ask the model with no evidence: what an unchecked answer looks like")
    b.add_argument("question")
    b.add_argument("--company", default="Sample Co", help="the company the questionnaire is for")
    b.add_argument("--provider", default="meta", choices=sorted(llm.PRESETS))
    b.set_defaults(fn=cmd_bare)
    r = sub.add_parser("review", help="approve or reject drafted answers")
    r.add_argument("answers")
    r.add_argument("--reviewer", required=True, help="who is reviewing (name or email); recorded in the audit log")
    r.set_defaults(fn=cmd_review)
    e = sub.add_parser("export", help="build a proof pack the customer can verify offline")
    e.add_argument("answers")
    e.add_argument("--package", help="the evidence package (default: the one recorded in the answers file)")
    e.add_argument("--out", default="out/proof-pack.zip")
    e.add_argument("--by", required=True, help="who is exporting (name or email); recorded in the audit log")
    e.add_argument("--allow-drafts", action="store_true", help="include unreviewed answers, marked DRAFT")
    e.set_defaults(fn=cmd_export)
    c = sub.add_parser("check", help="verify a proof pack (the same check as its verify_pack.py)")
    c.add_argument("pack")
    c.add_argument("--key", help="the company's published signing-key fingerprint: every batch must be signed by it")
    c.set_defaults(fn=cmd_check)
    ev = sub.add_parser("eval", help="score an answering mode against a labelled question set")
    ev.add_argument("evalset", nargs="?", default="evals/sample-co.json")
    ev.add_argument("--package", help="the evidence package (default: the one named in the eval set)")
    ev.add_argument("--mode", default="agent", choices=("agent", "no-verify", "rag", "retrieval"))
    ev.add_argument("--provider", default="meta", choices=sorted(llm.PRESETS))
    ev.add_argument("--verifier", choices=sorted(llm.PRESETS), help="a different provider for the verifier pass")
    ev.add_argument("--only", help="comma-separated item ids")
    ev.add_argument("--out-dir", default="evals/results")
    ev.add_argument("--fresh", action="store_true", help="start over instead of resuming an interrupted run of the same command")
    ev.set_defaults(fn=cmd_eval)
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # evidence titles contain dashes and names a Windows console code page cannot show
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        return args.fn(args)
    except llm.ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
