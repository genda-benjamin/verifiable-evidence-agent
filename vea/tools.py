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


class Toolbox:
    def __init__(self, package: EvidencePackage):
        self.package = package
        self._docs = {eid: Counter(_terms(e.text())) for eid, e in package.evidence.items()}
        self._df = Counter(t for doc in self._docs.values() for t in doc)
        self._n = max(1, len(self._docs))
        self.calls: list[dict[str, Any]] = []  # an audit trail of every tool call the agent made
        self.opened: set[str] = set()  # evidence ids whose full record the agent has read

    # ------------------------------------------------------------------ tools
    def search_evidence(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Rank checks by keyword relevance (BM25-style) and return each check's LATEST record.

        Every scan re-runs every check, so an older record of the same check is stale: citing it could report a
        failure that has since been fixed, or hide one that has since appeared. Search therefore never offers it."""
        q = _terms(query)
        best: dict[str, float] = {}
        for eid, doc in self._docs.items():
            length = sum(doc.values()) or 1
            score = 0.0
            for t in q:
                if t in doc:
                    idf = math.log(1 + (self._n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                    tf = doc[t]
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / 120))
            if score > 0:
                key = self.package.evidence[eid].check_key
                best[key] = max(best.get(key, 0.0), score)
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[: max(1, min(limit, 20))]
        out = []
        for key, score in ranked:
            e = self.package.evidence[self.package.latest[key]]
            out.append({"evidence_id": e.id, "title": e.title, "controls": e.control_ids,
                        "collected_at": e.record.get("collected_at"), "result": e.result,
                        "verified": e.verification.ok, "score": round(score, 2)})
        return out

    def get_evidence(self, evidence_id: str = "", evidence_ids: list[str] | None = None) -> Any:
        """Open one record (evidence_id) or up to 6 (evidence_ids). Only opened records may be cited."""
        ids = [evidence_id] if evidence_id else list(evidence_ids or [])[:6]
        if not ids:
            return {"error": "give evidence_id or evidence_ids"}
        out = []
        for i in ids:
            e = self.package.get(i)
            if e is None:
                out.append({"evidence_id": i, "error": "no evidence record with this id"})
                continue
            self.opened.add(e.id)
            item = e.summary()
            newer = self.package.superseded_by(e.id)
            if newer is not None:
                item["superseded_by"] = {"evidence_id": newer.id, "collected_at": newer.record.get("collected_at"),
                                         "result": newer.result, "note": "this record is stale; the newer one is the current state"}
            out.append(item)
        return out[0] if evidence_id else out

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
        "get_evidence": "get_evidence(evidence_id: str) or get_evidence(evidence_ids: [str, ...] up to 6): open full records: what was checked, the result, what was observed. You may cite only records you opened",
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
