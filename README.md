# Verifiable Evidence Agent

Answers security questionnaires **only with cryptographically cited evidence**. Built for the
[Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon) (Startup track,
"Build for Your World"), 26 October – 4 November 2026.

## The problem

Small companies in Kenya and across Africa lose enterprise and bank deals because they cannot credibly answer
100–300 question security questionnaires, and cannot afford consultants. Generic AI drafting makes this worse:
a confident answer with no evidence behind it is a liability in a compliance review.

## What this agent does

For each question in a customer's questionnaire, the agent:

1. **Researches** with read-only tools over the company's evidence package: search, open evidence records, read
   controls, list open findings.
2. **Drafts** an answer (Yes / No / Partially / Insufficient evidence) in which every claim cites evidence records.
3. **Checks every citation in code, without a model.** A cited record must exist, pass cryptographic verification
   (content hash, hash chain, Merkle inclusion proof), have been opened by the agent, and be the *latest* record of
   its check. A stale record can only be cited next to the newer one.
4. **Verifies** each surviving claim with a separate model call that sees only the claim and the evidence it cites,
   and drops claims the evidence does not support.
5. **Abstains** ("Insufficient evidence") when no claim survives, instead of guessing.
6. **Flags contradictions** for the reviewer, also in code: a "Yes" whose cited check currently fails, or with open
   findings on the controls it relies on.
7. Waits for **human approval** of every answer.
8. Exports a **proof pack**: the approved answers plus exactly the evidence records they cite, with their Merkle
   proofs. The customer opens `answers.html`, which re-verifies every citation in their browser, offline, or runs the
   bundled `verify_pack.py` (standard library only). They see only the evidence behind the answers, nothing else.

## What existed before the hackathon, and what is new

| | Status |
|---|---|
| **TrustEvidence** ([gettrustevidence.com](https://gettrustevidence.com)): the compliance platform that scans cloud accounts read-only, collects hash-chained evidence and exports evidence packages | **Existed before** the hackathon. Private codebase, not part of this submission. |
| **Everything in this repository** | **New**, built during the hackathon (first commit 26 October 2026). |

The agent reads TrustEvidence's existing evidence-package export format as its input. The verification code in
`vea/evidence.py` is a new, independent implementation of that format's published verification recipe.

`samples/sample-co-evidence-package.zip` is an export from TrustEvidence for a **fictional** company, Sample Co,
used as test data. Like every export, it contains TrustEvidence's own standalone verifier (`verify.py`,
`verify.html`) and report (`report.pdf`). Those files existed before the hackathon and are not part of the
submission.

## Quick start

Tested on Python 3.11. The agent uses only the standard library; the tests need `pytest`.

```bash
python -m vea verify samples/sample-co-evidence-package.zip
python -m pytest -q
```

Answering a questionnaire needs a model endpoint (Meta Model API, or Muse Spark Open on Baseten):

```bash
python -m vea answer samples/sample-co-evidence-package.zip samples/questionnaire.csv --provider meta
```

`python -m vea answer --help` lists the providers; each reads its key from an environment variable. Then:

```bash
python -m vea review out/answers.json                             # approve or reject each answer
python -m vea export out/answers.json --out out/proof-pack.zip   # build the pack for the customer
python -m vea check out/proof-pack.zip                            # what the customer runs
```

## Status

Under active development during the hackathon. Built so far: package verification, agent tools, the research loop,
the citation check, the verifier, the contradiction flags, human review and the proof pack, with tests. Next: an
evaluation set, the first runs on Meta models, questionnaire intake from PDFs and screenshots, and a web UI.

The proof pack's trust anchor is the sealed batch root. The sample package's batches are unsigned, so a recipient
must confirm the root with the company; packs from signed batches carry the signature, but the bundled verifier
does not check signatures yet.

## Security

API keys are never committed: they live in environment variables or an ignored `.env` file. Sample data in
this repository is synthetic.

## License

MIT. See [LICENSE](LICENSE).
