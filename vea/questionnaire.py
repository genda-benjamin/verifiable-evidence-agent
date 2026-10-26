"""Read questionnaires. CSV and plain text today; images and PDFs go through the vision intake."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Question:
    id: str
    text: str


def load(path: str | Path) -> list[Question]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return []
        keys = {k.lower().strip(): k for k in rows[0]}
        qcol = next((keys[k] for k in ("question", "questions", "text", "requirement") if k in keys), None)
        if qcol is None:
            raise ValueError(f"{path.name}: no 'question' column (found: {', '.join(rows[0])})")
        icol = keys.get("id")
        return [Question(str(r[icol]).strip() if icol else f"Q{n}", r[qcol].strip())
                for n, r in enumerate(rows, 1) if (r.get(qcol) or "").strip()]
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
    return [Question(f"Q{n}", l) for n, l in enumerate((l for l in lines if l), 1)]
