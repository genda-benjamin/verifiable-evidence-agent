# Verifiable Evidence Agent

Answers security questionnaires **only with cryptographically cited evidence**. Built for the
[Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon) (Startup track,
"Build for Your World"), 26 October – 4 November 2026.

## The problem

Small companies in Kenya and across Africa lose enterprise and bank deals because they cannot credibly answer
100–300 question security questionnaires, and cannot afford consultants. Generic AI drafting makes this worse:
a confident answer with no evidence behind it is a liability in a compliance review.

## What this agent does

For each question in a customer's questionnaire (PDF, spreadsheet or screenshot), the agent:

1. **Plans** what evidence would prove an answer.
2. **Retrieves** evidence through tools over the company's own evidence package.
3. **Drafts** an answer in which every claim cites an evidence record by its tamper-evident hash.
4. **Verifies** each claim with a second model pass and rejects anything the cited evidence does not support.
5. **Abstains** ("not enough evidence") and says what is missing, instead of guessing.
6. Waits for **human approval** of every answer before export.

## What existed before the hackathon, and what is new

| | Status |
|---|---|
| **TrustEvidence** ([gettrustevidence.com](https://gettrustevidence.com)): the compliance platform that scans cloud accounts read-only, collects hash-chained evidence and exports evidence packages | **Existed before** the hackathon. Private codebase, not part of this submission. |
| **Everything in this repository** | **New**, built during the hackathon (first commit 26 October 2026). |

The agent reads TrustEvidence's existing evidence-package export format as its input. No TrustEvidence code is
copied here.

## Status

Day 0: repository set up. The build starts 27 October.

## Security

API keys are never committed: they live in environment variables or an ignored `.env` file. Sample data in
this repository is synthetic.

## License

MIT. See [LICENSE](LICENSE).
