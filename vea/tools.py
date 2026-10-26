"""The agent's tools: read-only views over one evidence package.

Every tool returns plain JSON-able data. Evidence that failed cryptographic verification is never returned as
citable: search results and lookups mark it, and the agent's final check rejects it.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from vea.evidence import EvidencePackage

_WORD = re.compile(r"[a-z0-9]+")
# Words that carry no meaning in questionnaire language and would only add noise to keyword search.
_STOP = set("a an and are as at be by do does for from has have how in is it of on or that the to was what when "
            "which with you your yes no all any our we us please describe provide".split())


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


def _result(content: dict[str, Any]) -> Any:
    r = content.get("result")
    return r.get("status") if isinstance(r, dict) else r


class Toolbox:
    def __init__(self, package: EvidencePackage):
        self.package = package
        self._docs = {eid: Counter(_terms(e.text())) for eid, e in package.evidence.items()}
        self._df = Counter(t for doc in self._docs.values() for t in doc)
        self._n = max(1, len(self._docs))
        self.calls: list[dict[str, Any]] = []  # an audit trail of every tool call the agent made

    # ------------------------------------------------------------------ tools
    def search_evidence(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Rank evidence records by keyword relevance (BM25-style), newest first on ties."""
        q = _terms(query)
        scored = []
        for eid, doc in self._docs.items():
            length = sum(doc.values()) or 1
            score = 0.0
            for t in q:
                if t in doc:
                    idf = math.log(1 + (self._n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                    tf = doc[t]
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / 120))
            if score > 0:
                e = self.package.evidence[eid]
                scored.append((score, e.record.get("collected_at", ""), e))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        # The same check runs on every scan; show only its latest record so the results are not all duplicates.
        seen: dict[str, dict[str, Any]] = {}
        for s, _, e in scored:
            if e.title in seen:
                seen[e.title]["earlier_records"] += 1
                continue
            seen[e.title] = {"evidence_id": e.id, "title": e.title, "controls": e.control_ids,
                             "collected_at": e.record.get("collected_at"), "result": _result(e.content),
                             "verified": e.verification.ok, "score": round(s, 2), "earlier_records": 0}
            if len(seen) >= max(1, min(limit, 20)) + 5:
                break
        return list(seen.values())[: max(1, min(limit, 20))]

    def get_evidence(self, evidence_id: str) -> dict[str, Any]:
        e = self.package.get(evidence_id)
        if e is None:
            return {"error": f"no evidence record with id {evidence_id}"}
        return e.summary()

    def get_control(self, control_id: str) -> dict[str, Any]:
        for c in self.package.controls:
            if c.get("control_id") == control_id:
                out = {k: c.get(k) for k in ("control_id", "name", "objective", "status", "applicable", "criteria",
                                             "justification", "open_findings", "last_tested") if c.get(k) not in (None, "")}
                out["checks"] = [{"check_id": x.get("check_id"), "status": x.get("status"), "expected": x.get("expected"),
                                  "evidence_id": x.get("evidence_id"), "summary": x.get("summary")} for x in (c.get("checks") or [])]
                if c.get("review"):
                    r = c["review"]
                    out["human_review"] = {k: r.get(k) for k in ("result", "evidence_id", "reviewed_at", "valid_until", "expired", "notes")}
                return out
        return {"error": f"no control {control_id}"}

    def list_controls(self) -> list[dict[str, Any]]:
        return [{"control_id": c.get("control_id"), "name": c.get("name"), "status": c.get("status")} for c in self.package.controls]

    def list_findings(self, status: str = "open") -> list[dict[str, Any]]:
        """Findings by their status at the end of the period: 'open' (open or in progress), 'resolved', or 'any'."""
        wanted = {"open", "in_progress"} if status == "open" else None if status == "any" else {status}
        out = []
        for f in self.package.findings:
            if wanted is None or f.get("status_at_period_end") in wanted:
                out.append({"title": f.get("title"), "severity": f.get("severity"), "status": f.get("status_at_period_end"),
                            "controls": f.get("controls"), "resource": f.get("resource"), "due_date": f.get("due_date"),
                            "evidence_id": f.get("latest_evidence_id")})
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        return sorted(out, key=lambda f: order.get(f["severity"] or "", 9))[:30]

    # ------------------------------------------------------------------ dispatch
    SPECS = {
        "search_evidence": "search_evidence(query: str, limit: int = 8): find evidence records relevant to a topic",
        "get_evidence": "get_evidence(evidence_id: str): read one evidence record: what was checked, the result, what was observed",
        "get_control": "get_control(control_id: str): a control's status and the evidence ids behind it (e.g. TE-AC-01)",
        "list_controls": "list_controls(): every control with its status",
        "list_findings": "list_findings(status: str = 'open'): security findings (problems found); status is 'open', 'resolved' or 'any'",
    }

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name not in self.SPECS:
            result: Any = {"error": f"unknown tool {name}; available: {', '.join(self.SPECS)}"}
        else:
            try:
                result = getattr(self, name)(**(args or {}))
            except TypeError as exc:
                result = {"error": f"bad arguments for {name}: {exc}"}
        self.calls.append({"tool": name, "args": args})
        return result
