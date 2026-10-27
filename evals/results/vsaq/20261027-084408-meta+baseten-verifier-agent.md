# Evaluation: agent on muse-spark-1.3

Provider `meta`, verifier `meta-models/muse-spark-open-1026`. 929.1 s, 195 model calls, 575151 prompt / 97498 completion tokens.

| Metric | Value |
|---|---|
| Questions | 29 |
| Answer accuracy | 86.2% |
| Grounded accuracy (right answer and required evidence cited) | 82.8% |
| Unsafe answers (would mislead a customer) | 0 (0.0%) |
| Citation recall on correct answers | 93.3% |
| Correct abstentions (no evidence exists) | 83.3% |
| False abstentions (evidence exists) | 30.4% |
| Answers the checks turned into abstentions | 0 |
| Claims kept / rejected | 47 / 1 |
| Rejection reasons | verifier: unsupported: 1 |
| Answers flagged for the reviewer | 2 |
| Questions the model provider refused (scored as wrong) | 2 |
| Refusals that name what is missing | 100.0% |

| Category | n | Accuracy | Grounded | Unsafe |
|---|---|---|---|---|
| fail | 2 | 100.0% | 50.0% | 0 |
| human-evidence | 10 | 80.0% | 80.0% | 0 |
| incomplete | 6 | 83.3% | 83.3% | 0 |
| no-evidence | 6 | 83.3% | 83.3% | 0 |
| pass | 5 | 100.0% | 100.0% | 0 |

| Id | Answer | Expected | Result |
|---|---|---|---|
| V01 | Yes | Yes / Partially | grounded |
| V02 | Insufficient evidence | Insufficient evidence / Partially | grounded |
| V03 | Yes | Yes | grounded |
| V04 | Insufficient evidence | Insufficient evidence / Partially | grounded |
| V05 | Insufficient evidence | Insufficient evidence | grounded |
| V06 | Yes | Yes / Partially | grounded |
| V07 | Partially | Yes / Partially | grounded |
| V08 | Partially | Insufficient evidence | wrong |
| V09 | Partially | Yes / Partially | grounded |
| V10 | Insufficient evidence | Insufficient evidence / Partially | grounded |
| V11 | Yes | Yes / Partially | grounded |
| V12 | Insufficient evidence | Insufficient evidence / No | grounded |
| V13 | Partially | Partially / No / Insufficient evidence | grounded |
| V14 | Yes | Yes / Partially | grounded |
| V15 | Yes | Yes | grounded |
| V16 | Yes | Yes | grounded |
| V17 | Insufficient evidence | Insufficient evidence | grounded |
| V18 | Insufficient evidence | Partially / Insufficient evidence | grounded |
| V19 | Insufficient evidence | Partially / Insufficient evidence | refused by the model provider |
| V20 | Partially | Yes | wrong |
| V21 | Partially | Partially / Yes | grounded |
| V22 | Yes | Yes | grounded |
| V23 | Yes | Yes | grounded |
| V24 | Partially | Yes / Partially | grounded |
| V25 | Insufficient evidence | Yes | refused by the model provider |
| V26 | Partially | Partially / No | correct, citation missing |
| V27 | Insufficient evidence | Insufficient evidence | grounded |
| V28 | Insufficient evidence | Insufficient evidence | grounded |
| V29 | Insufficient evidence | Insufficient evidence | grounded |
