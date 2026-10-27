# VSAQ questions

`evals/vsaq.json` contains 29 questions copied from Google's **Vendor Security Assessment Questionnaires (VSAQ)**,
real questionnaires Google published for assessing the security of third-party vendors.

- Source: https://github.com/google/vsaq, commit `366d670e7e2a544166f74182e1093e6162786eb7`, files
  `questionnaires/infrastructure.json` and `questionnaires/security_privacy_programs.json`
- Copyright 2016 Google Inc.
- Licensed under the Apache License, Version 2.0; a copy is in [LICENSE](LICENSE) next to this file.

Changes made for this project: 29 of the 90 multiple-choice questions were selected, their HTML tags were removed and
whitespace normalised, and each was given an id (`V01`–`V29`), a category, and labels (acceptable answers, unsafe
answers, required evidence) for the fictional Sample Co evidence package. The original question id is kept in each
item's `source` field. The labels are this project's work, not Google's, and Google does not endorse this project.

`samples/questionnaire.pdf` and the images made from it (`samples/make_questionnaire.py`) also contain 8 of these
questions, word for word, in a fictional bank's questionnaire layout; each is marked `"source": "VSAQ"` in
`samples/questionnaire-truth.json`.
