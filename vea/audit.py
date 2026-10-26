"""Append-only, hash-chained audit log of who drafted, approved, rejected and exported each answer.

Each entry records its sequence number, time, actor, action, the question it concerns, details, and the hash of the
entry before it; its own hash covers all of that. Editing, removing or reordering any entry breaks every hash after
it, which `verify` reports.

Approvals record the fingerprint (`proofcheck.answer_digest`) of the exact answer the reviewer saw, so an answer
edited after approval no longer matches its approval, and export refuses it.

When the agent runs inside TrustEvidence, these events go to TrustEvidence's own audit log instead; this file is the
standalone equivalent for the command line.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from vea.proofcheck import answer_digest, entry_hash

GENESIS = "GENESIS"


class Approvals(Protocol):
    """Where approvals are recorded. The proof pack needs only these three things from it."""

    def approval_problems(self, results: list[dict[str, Any]]) -> list[str]: ...
    def latest_decision(self, subject: str) -> dict[str, Any] | None: ...
    def exported(self, actor: str, pack_sha256: str, question_ids: list[str]) -> Any: ...


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def append(self, actor: str, action: str, subject: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        if not actor.strip():
            raise ValueError("every audit entry needs an actor")
        entries = self.entries()
        entry = {"seq": len(entries) + 1, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "actor": actor.strip(), "action": action, "subject": subject, "details": details or {},
                 "prev": entries[-1]["hash"] if entries else GENESIS}
        entry["hash"] = entry_hash(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return entry

    def verify(self) -> list[str]:
        problems, prev = [], GENESIS
        for n, e in enumerate(self.entries(), 1):
            if e.get("seq") != n:
                problems.append(f"entry {n}: sequence number is {e.get('seq')}, expected {n} (entries removed or reordered)")
            if e.get("prev") != prev:
                problems.append(f"entry {n}: does not link to the entry before it")
            if e.get("hash") != entry_hash(e):
                problems.append(f"entry {n}: contents changed after it was written")
            prev = e.get("hash")
        return problems

    def latest_decision(self, subject: str) -> dict[str, Any] | None:
        """The most recent approval or rejection of this question, if any."""
        for e in reversed(self.entries()):
            if e.get("subject") == subject and e.get("action") in ("approved", "rejected"):
                return e
        return None

    # ------------------------------------------------------------------ the events the workflow records
    def drafted(self, actor: str, result: dict[str, Any]) -> dict[str, Any]:
        cited = sorted({i for c in result.get("claims") or [] for i in c.get("evidence_ids") or []})
        return self.append(actor, "drafted", result["id"], {"answer": result.get("answer"), "digest": answer_digest(result),
                                                            "evidence_ids": cited,
                                                            "claims_rejected": len(result.get("rejected_claims") or [])})

    def decided(self, reviewer: str, result: dict[str, Any], approve: bool, note: str = "") -> dict[str, Any]:
        return self.append(reviewer, "approved" if approve else "rejected", result["id"],
                           {"digest": answer_digest(result), "answer": result.get("answer"), "note": note})

    def exported(self, actor: str, pack_sha256: str, question_ids: list[str]) -> dict[str, Any]:
        return self.append(actor, "exported", "pack", {"pack_sha256": pack_sha256, "questions": question_ids})

    def approval_problems(self, results: list[dict[str, Any]]) -> list[str]:
        """Why export must refuse: a broken log, or a shipped approval that does not match the answer as it is now."""
        problems = self.verify()
        for r in results:
            if r.get("status") != "approved":
                continue
            d = self.latest_decision(r["id"])
            if d is None or d["action"] != "approved":
                problems.append(f"{r['id']}: marked approved, but the audit log has no approval for it")
            elif d["details"].get("digest") != answer_digest(r):
                problems.append(f"{r['id']}: the answer changed after it was approved; review it again")
        return problems


def approval_entry(actor: str, subject: str, answer: dict[str, Any], *, at: str, ref: str, note: str = "",
                   digest: str | None = None) -> dict[str, Any]:
    """An approval in the proof-pack format, for a host application that keeps approvals in its own audit log.

    `ref` names the host's own record of the approval (for example "trustevidence-audit:<event id>"), and stands where
    the standalone log keeps the previous entry's hash. The host's log provides the append-only guarantee; this entry
    binds the approval to the exact answer, so the recipient can still check it. Pass the `digest` stored when the
    person approved: recomputing it from today's answer would hide an edit made after approval."""
    entry = {"seq": 0, "at": at, "actor": actor, "action": "approved", "subject": subject,
             "details": {"digest": digest or answer_digest(answer), "answer": answer.get("answer"), "note": note}, "prev": ref}
    entry["hash"] = entry_hash(entry)
    return entry


class RecordedApprovals:
    """Approvals kept in a host application's audit log (append-only there), handed to the proof pack builder."""

    def __init__(self, entries: dict[str, dict[str, Any]], on_export: Callable[[str, str, list[str]], Any]):
        self.entries = entries  # question id -> approval entry, the latest approval of each answer
        self.on_export = on_export

    def latest_decision(self, subject: str) -> dict[str, Any] | None:
        return self.entries.get(subject)

    def approval_problems(self, results: list[dict[str, Any]]) -> list[str]:
        problems = []
        for r in results:
            if r.get("status") != "approved":
                continue
            e = self.entries.get(r["id"])
            if e is None:
                problems.append(f"{r['id']}: marked approved, but the audit log has no approval for it")
            elif e.get("hash") != entry_hash(e):
                problems.append(f"{r['id']}: the approval record was altered")
            elif e["details"].get("digest") != answer_digest(r):
                problems.append(f"{r['id']}: the answer changed after it was approved; review it again")
        return problems

    def exported(self, actor: str, pack_sha256: str, question_ids: list[str]) -> Any:
        return self.on_export(actor, pack_sha256, question_ids)
