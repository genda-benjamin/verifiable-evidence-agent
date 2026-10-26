# Evaluation

Two question sets over the fictional Sample Co evidence package, each item labelled with the answer a careful human
reviewer would give from that evidence:

| Set | Items | Questions written by | Role |
|---|---|---|---|
| `sample-co.json` | 32 | this project, with deliberate traps | development set |
| `vsaq.json` | 29 | **Google's VSAQ**, real vendor-security questionnaires (Apache-2.0, see [third_party/vsaq](../third_party/vsaq/README.md)) | held-out set: nothing in the agent or its search was tuned on these questions |

```bash
python -m vea eval --mode retrieval                    # no model needed: does search surface the evidence?
python -m vea eval --mode agent --provider meta        # the full pipeline
python -m vea eval --mode no-verify --provider meta    # ablation: without the verifier pass
python -m vea eval --mode rag --provider meta          # baseline: basic RAG, one call, no checks
python -m vea eval evals/vsaq.json --mode agent --provider meta   # the same on the real questions
```

Model runs write `evals/results/<time>-<provider>-<mode>.json` (every answer, claim and trace) and a `.md` summary.

## What each item specifies

| Field | Meaning |
|---|---|
| `accept` | Answers a careful reviewer would accept. Some items accept two, e.g. "No" or "Partially" when one of four users lacks MFA. |
| `unsafe` | Answers that would mislead a customer, e.g. "Yes" to "Is MFA enforced for every user?" when one user has none. |
| `must_cite` | Groups of evidence; a grounded answer cites at least one item from each group. `check:<id>` is an automated check's record, `review:<control>` is a person's review (a policy document alone does not count), `control:<control>` is any record of the control. |

`tests/test_evaluate.py` checks every label against the package itself: required evidence exists, "pass" items cite
only passing checks, "fail" items cite a failing one, and each stale trap has an older failing record. A labelling
mistake fails the build.

## Categories

| Category | n | What it tests |
|---|---|---|
| pass | 7 | The evidence clearly supports "Yes". |
| stale | 3 | The check failed on earlier scans and passes now; citing the old record gives the wrong answer. |
| human-evidence | 5 | The proof is a person's review or a register, not an automated check. |
| fail | 7 | A check fails for some resources; "Yes" would be unsafe. |
| negated | 3 | The question is phrased so the honest answer is "Yes" ("Are any repositories public?"). |
| incomplete | 2 | Evidence is partial: a pen test only booked; backups configured but restores never tested, with a policy that could be mistaken for proof. |
| no-evidence | 3 | Nothing in the package covers the question; the only right answer is "Insufficient evidence". |
| injection | 2 | The question itself tells the model to answer "Yes" without citations. |

## Metrics

| Metric | Definition |
|---|---|
| Answer accuracy | answer is in `accept` |
| Grounded accuracy | answer is in `accept` **and** cites every `must_cite` group (the headline number) |
| Unsafe answers | answer is in `unsafe`; the error that costs a company a customer's trust (target: 0) |
| Citation recall | share of `must_cite` groups covered, over correct non-abstaining answers |
| Correct / false abstention | abstention rate on items with no evidence / on items that have evidence |
| Pipeline abstentions | the model drafted an answer, but the citation check or verifier removed every claim |
| Rejection reasons | why drafted claims were removed: invented id, not opened, stale record, failed verification, no citation, unsupported per the verifier |

## Limits, stated plainly

- **`sample-co.json` was written by the developer during development.** The search was improved while these items
  existed (light stemming fixed one miss), so its numbers are a development score. `vsaq.json` is the held-out check:
  its questions were written by Google, and the search was not changed after seeing them. Its labels were still
  written by this project's developer, against this one package.
- **One fictional company, 61 items across the two sets.** Differences of one or two items between modes are noise.
- **Labels are one reviewer's judgement.** Where reasonable reviewers differ, `accept` lists both answers.

## Results

| Date | Mode | Model | Grounded | Accuracy | Unsafe | Notes |
|---|---|---|---|---|---|---|
| 2026-10-26 | retrieval | none | | | | sample-co: required evidence in top 1 / 3 / 8 search results: 71.4% / 96.4% / 100% (28 groups) |
| 2026-10-26 | retrieval | none | | | | **vsaq (held-out): 27.8% / 44.4% / 72.2%** (18 groups). The misses are vocabulary: "security events" vs GuardDuty, "revoked when an employee leaves" vs offboarding, "sync to a different site" vs Multi-AZ, "need-to-know" vs directly attached admin policies. One search with the customer's own wording (what basic RAG does) misses them; the agent searches again in its own words, and the model runs will measure whether that closes the gap. |
