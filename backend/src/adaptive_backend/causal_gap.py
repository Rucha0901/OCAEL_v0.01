from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _label(cause: str) -> str:
    return {
        "direct": "Concept foundation",
        "prerequisite": "Prerequisite dependency",
        "misconception": "Persistent misconception",
        "retention": "Retention decay",
        "transfer": "Transfer difficulty",
        "fragile_mastery": "Fragile mastery",
        "assessment_uncertainty": "Evidence quality",
        "insufficient_evidence": "More evidence needed",
    }.get(cause, cause.replace("_", " ").title())


class CausalGapEngine:
    """Deterministic, auditable causal-gap projection.

    It reads Sam-owned learner state and the immutable event ledger. It never
    mutates mastery, selects pedagogy, or treats heuristic support as a
    calibrated probability.
    """

    CALIBRATION_STATE = "heuristic_v1"

    def __init__(self, db: Database):
        self.db = db

    def analyze(
        self,
        *,
        user_id: str,
        case_owner_user_id: str | None = None,
        subject_id: str | None = None,
        max_cases: int = 6,
        persist: bool = True,
    ) -> dict[str, Any]:
        case_owner_user_id = case_owner_user_id or user_id
        clauses = ["s.user_id=?"]
        params: list[Any] = [user_id]
        if subject_id:
            clauses.append("c.subject_id=?")
            params.append(subject_id)
        states = self.db.fetchall(
            f"""
            SELECT s.*, c.name AS concept_name, c.subject_id
            FROM concept_state s
            JOIN concepts c ON c.concept_id=s.concept_id
            WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        )

        candidates: list[dict[str, Any]] = []
        for state in states:
            mastery = float(state["mastery_belief"])
            status = str(state["status"])
            recurrence = int(state["recurrence_count"])
            evidence_count = int(state["evidence_count"])
            # An untouched concept is not a learning gap. Treating unknown,
            # evidence-free catalog entries as cases made every subject appear
            # to have the same fixed number of weaknesses.
            if evidence_count <= 0 and recurrence <= 0:
                continue
            if status not in {"active_blocker", "needs_review", "developing", "stale"} and mastery >= 0.72 and recurrence == 0:
                continue
            case = self._build_case(
                evidence_user_id=user_id,
                case_owner_user_id=case_owner_user_id,
                state=state,
            )
            candidates.append(case)

        candidates.sort(key=lambda item: (-float(item["focus_priority"]), item["concept_name"]))
        cases = candidates[: max(1, min(12, max_cases))]
        total_priority = sum(float(case["focus_priority"]) for case in cases) or 1.0
        for case in cases:
            case["focus_allocation_pct"] = round(100 * float(case["focus_priority"]) / total_priority)
            case["recommended_minutes"] = max(10, min(40, round(10 + 30 * float(case["focus_priority"]))))
            case["next_step"] = self._next_step(case)

        allocations = sum(int(case["focus_allocation_pct"]) for case in cases)
        if cases and allocations != 100:
            cases[0]["focus_allocation_pct"] += 100 - allocations

        # Persist only after the rounding correction so the immutable ledger
        # exactly matches the allocation returned to the learner.
        if persist:
            self._reconcile_scope(
                user_id=case_owner_user_id,
                subject_id=subject_id,
                active_case_ids={str(case["case_id"]) for case in cases},
            )
            for case in cases:
                self._persist_case(case_owner_user_id, case)

        return {
            "generated_at": _now(),
            "calibration_state": self.CALIBRATION_STATE,
            "subject_id": subject_id,
            "summary": {
                "case_count": len(cases),
                "high_priority_count": sum(case["severity"] == "high" for case in cases),
                "abstained_count": sum(bool(case["abstained"]) for case in cases),
                "headline": self._headline(cases),
                "privacy": "Derived evidence only; raw learner responses are never returned.",
            },
            "cases": cases,
        }

    def list_cases(self, *, user_id: str, subject_id: str | None = None) -> dict[str, Any]:
        clauses = ["c.user_id=?", "c.status!='dismissed'"]
        params: list[Any] = [user_id]
        if subject_id:
            clauses.append("c.subject_id=?")
            params.append(subject_id)
        rows = self.db.fetchall(
            f"""
            SELECT r.snapshot_json
            FROM causal_gap_cases c
            JOIN causal_gap_revisions r ON r.revision_id=c.current_revision_id
            WHERE {' AND '.join(clauses)}
            ORDER BY c.focus_priority DESC, c.updated_at DESC
            """,
            tuple(params),
        )
        cases = [self.db.loads(row["snapshot_json"], {}) for row in rows]
        return {
            "generated_at": _now(),
            "calibration_state": self.CALIBRATION_STATE,
            "subject_id": subject_id,
            "summary": {
                "case_count": len(cases),
                "high_priority_count": sum(case.get("severity") == "high" for case in cases),
                "abstained_count": sum(bool(case.get("abstained")) for case in cases),
                "headline": self._headline(cases),
                "privacy": "Derived evidence only; raw learner responses are never returned.",
            },
            "cases": cases,
        }

    def get_case(self, *, user_id: str, case_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            """
            SELECT r.snapshot_json
            FROM causal_gap_cases c
            JOIN causal_gap_revisions r ON r.revision_id=c.current_revision_id
            WHERE c.case_id=? AND c.user_id=?
            """,
            (case_id, user_id),
        )
        return self.db.loads(row["snapshot_json"], {}) if row else None

    def challenge(self, *, user_id: str, case_id: str, reason: str) -> dict[str, Any] | None:
        case = self.get_case(user_id=user_id, case_id=case_id)
        if not case:
            return None
        case["status"] = "challenged"
        case["challenge"] = {
            "reason": reason.strip()[:500],
            "recorded_at": _now(),
            "effect": "IRIS will require fresh independent evidence before strengthening this cause.",
        }
        case["abstained"] = True
        case["confidence"] = min(float(case.get("confidence", 0.0)), 0.45)
        case["learner_message"] = "You flagged this explanation. OVAEL will treat it as uncertain and verify it again rather than assuming it is correct."
        self._persist_case(user_id, case, force_revision=True)
        return case

    def _build_case(
        self,
        *,
        evidence_user_id: str,
        case_owner_user_id: str,
        state: Any,
    ) -> dict[str, Any]:
        concept_id = str(state["concept_id"])
        subject_id = str(state["subject_id"])
        mastery = float(state["mastery_belief"])
        confidence = float(state["confidence"])
        evidence_count = int(state["evidence_count"])
        recurrence = int(state["recurrence_count"])
        events = self.db.fetchall(
            """
            SELECT correct,score,evidence_strength,input_mode,mistake_type,metadata_json,timestamp
            FROM learning_events
            WHERE user_id=? AND primary_concept_id=?
            ORDER BY timestamp DESC LIMIT 24
            """,
            (evidence_user_id, concept_id),
        )
        hypotheses = self.db.fetchall(
            """
            SELECT h.*, c.name AS gap_name
            FROM gap_hypotheses h
            LEFT JOIN concepts c ON c.concept_id=h.gap_concept_id
            WHERE h.user_id=? AND h.target_concept_id=?
            ORDER BY h.probability DESC LIMIT 6
            """,
            (evidence_user_id, concept_id),
        )
        upstream = self.db.fetchall(
            """
            SELECT e.source_concept_id AS concept_id, c.name, e.relation, e.strength
            FROM concept_edges e JOIN concepts c ON c.concept_id=e.source_concept_id
            WHERE e.target_concept_id=? LIMIT 4
            """,
            (concept_id,),
        )
        downstream = self.db.fetchall(
            """
            SELECT e.target_concept_id AS concept_id, c.name, e.relation, e.strength
            FROM concept_edges e JOIN concepts c ON c.concept_id=e.target_concept_id
            WHERE e.source_concept_id=? LIMIT 6
            """,
            (concept_id,),
        )

        evidence_quality, independent_count = self._evidence_quality(events)
        wrong_events = [event for event in events if event["correct"] == 0 or (event["score"] is not None and float(event["score"]) < 0.55)]
        mistakes = [str(event["mistake_type"]) for event in wrong_events if event["mistake_type"]]
        top_hypothesis = hypotheses[0] if hypotheses else None
        cause = self._cause(
            state=state,
            events=events,
            mistakes=mistakes,
            top_hypothesis=top_hypothesis,
            evidence_quality=evidence_quality,
            independent_count=independent_count,
        )

        deficit = 1.0 - mastery
        recurrence_signal = min(1.0, recurrence / 3.0)
        downstream_signal = min(1.0, len(downstream) / 4.0)
        status_signal = 1.0 if state["status"] == "active_blocker" else 0.72 if state["status"] in {"needs_review", "stale"} else 0.5
        focus_priority = _clamp(0.43 * deficit + 0.20 * recurrence_signal + 0.17 * downstream_signal + 0.20 * status_signal)
        hypothesis_support = float(top_hypothesis["probability"]) if top_hypothesis else 0.0
        support = _clamp(0.25 + 0.34 * deficit + 0.14 * recurrence_signal + 0.17 * hypothesis_support + 0.10 * evidence_quality)
        diagnostic_confidence = _clamp(0.15 + 0.35 * evidence_quality + 0.25 * min(1.0, evidence_count / 6.0) + 0.25 * support)
        abstained = evidence_count < 2 or independent_count < 1 or evidence_quality < 0.35
        if cause in {"insufficient_evidence", "assessment_uncertainty"}:
            support = min(support, 0.42)
            diagnostic_confidence = min(diagnostic_confidence, 0.45)
            abstained = True

        severity = "high" if focus_priority >= 0.68 else "medium" if focus_priority >= 0.42 else "watch"
        case_id = str(uuid5(NAMESPACE_URL, f"ovael:causal-gap:{case_owner_user_id}:{concept_id}"))
        evidence = self._evidence_summary(
            state=state,
            events=events,
            mistakes=mistakes,
            independent_count=independent_count,
            evidence_quality=evidence_quality,
            top_hypothesis=top_hypothesis,
        )
        graph = self._micrograph(
            concept_id=concept_id,
            concept_name=str(state["concept_name"]),
            state=state,
            cause=cause,
            support=support,
            upstream=upstream,
            downstream=downstream,
            top_hypothesis=top_hypothesis,
        )
        return {
            "case_id": case_id,
            "status": "open",
            "subject_id": subject_id,
            "target_concept_id": concept_id,
            "concept_name": str(state["concept_name"]),
            "concept_state": str(state["status"]),
            "mastery_belief": round(mastery, 4),
            "state_confidence": round(confidence, 4),
            "evidence_count": evidence_count,
            "recurrence_count": recurrence,
            "cause_type": cause,
            "cause_label": _label(cause),
            "normalized_support": round(support, 4),
            "confidence": round(diagnostic_confidence, 4),
            "calibration_state": self.CALIBRATION_STATE,
            "abstained": abstained,
            "severity": severity,
            "focus_priority": round(focus_priority, 4),
            "focus_allocation_pct": 0,
            "recommended_minutes": 0,
            "learner_message": self._learner_message(str(state["concept_name"]), cause, abstained),
            "evidence_quality": {
                "score": round(evidence_quality, 4),
                "independent_attempts": independent_count,
                "raw_response_returned": False,
            },
            "evidence_for": evidence,
            "evidence_against": self._counter_evidence(state=state, events=events),
            "falsification_check": self._falsification(cause, str(state["concept_name"])),
            "micrograph": graph,
        }

    @staticmethod
    def _evidence_quality(events: list[Any]) -> tuple[float, int]:
        if not events:
            return 0.0, 0
        quality_values: list[float] = []
        independent = 0
        for event in events:
            metadata = json.loads(event["metadata_json"] or "{}")
            behavior = metadata.get("behavior") if isinstance(metadata.get("behavior"), dict) else {}
            hints = int(metadata.get("hint_count", behavior.get("hint_count", 0)) or 0)
            attempts = int(metadata.get("attempt_count", behavior.get("attempt_count", 1)) or 1)
            strength = float(event["evidence_strength"] or 0.0)
            mode_factor = 0.68 if event["input_mode"] == "ocr" and metadata.get("ocr_confidence") is None else 1.0
            q = strength * mode_factor * (1.0 / (1.0 + 0.18 * hints + 0.08 * max(0, attempts - 1)))
            quality_values.append(_clamp(q))
            if hints == 0 and attempts == 1 and event["correct"] is not None:
                independent += 1
        return sum(quality_values) / len(quality_values), independent

    @staticmethod
    def _cause(*, state: Any, events: list[Any], mistakes: list[str], top_hypothesis: Any, evidence_quality: float, independent_count: int) -> str:
        if int(state["evidence_count"]) < 2 or len(events) < 2 or independent_count < 1:
            return "insufficient_evidence"
        if evidence_quality < 0.45:
            return "assessment_uncertainty"
        if top_hypothesis and top_hypothesis["source"] in {"prerequisite", "component"}:
            return "prerequisite"
        if top_hypothesis and top_hypothesis["source"] == "misconception":
            return "misconception"
        if mistakes and max(mistakes.count(item) for item in set(mistakes)) >= 2:
            return "misconception"
        transfer_misses = 0
        for event in events:
            metadata = json.loads(event["metadata_json"] or "{}")
            if metadata.get("transfer") or metadata.get("application_context"):
                if event["correct"] == 0 or (event["score"] is not None and float(event["score"]) < 0.55):
                    transfer_misses += 1
        if transfer_misses >= 2 and float(state["mastery_belief"]) >= 0.55:
            return "transfer"
        if int(state["recurrence_count"]) > 0 and float(state["mastery_belief"]) >= 0.55:
            return "fragile_mastery"
        if state["status"] == "stale":
            return "retention"
        return "direct"

    @staticmethod
    def _evidence_summary(*, state: Any, events: list[Any], mistakes: list[str], independent_count: int, evidence_quality: float, top_hypothesis: Any) -> list[dict[str, Any]]:
        out = [
            {
                "signal": "learner_state",
                "detail": f"{state['status'].replace('_', ' ')} with {int(state['evidence_count'])} observations",
                "strength": round(float(state["confidence"]), 3),
            },
            {
                "signal": "independent_attempts",
                "detail": f"{independent_count} unhinted first attempts in the retained evidence window",
                "strength": round(evidence_quality, 3),
            },
        ]
        if mistakes:
            common = max(set(mistakes), key=mistakes.count)
            out.append({"signal": "recurring_error", "detail": common.replace("_", " "), "strength": round(min(1.0, mistakes.count(common) / 3), 3)})
        if top_hypothesis:
            out.append({
                "signal": "sam_hypothesis",
                "detail": f"{top_hypothesis['source']} signal points to {top_hypothesis['gap_name'] or top_hypothesis['gap_concept_id']}",
                "strength": round(float(top_hypothesis["probability"]), 3),
            })
        if len(events) == 0:
            out.append({"signal": "missing_evidence", "detail": "No retained attempts are available", "strength": 1.0})
        return out[:4]

    @staticmethod
    def _counter_evidence(*, state: Any, events: list[Any]) -> list[str]:
        correct = sum(event["correct"] == 1 for event in events)
        out: list[str] = []
        if correct:
            out.append(f"{correct} retained attempts were successful, so the concept is not treated as globally absent.")
        if float(state["mastery_belief"]) >= 0.65:
            out.append("Mastery belief remains above the developing threshold; IRIS is checking fragility rather than declaring failure.")
        if not out:
            out.append("No strong counter-signal is available yet; the next independent check can change this case.")
        return out

    @staticmethod
    def _learner_message(concept_name: str, cause: str, abstained: bool) -> str:
        if abstained:
            return f"This is not a verdict about your ability in {concept_name}. OVAEL needs one clean check before deciding what is actually getting in the way."
        messages = {
            "prerequisite": f"Your work on {concept_name} suggests one earlier idea may be carrying too much load. A short rewind should make the main topic feel lighter.",
            "misconception": f"You are making progress on {concept_name}; one repeatable interpretation appears to be pulling answers off course.",
            "retention": f"You learned parts of {concept_name} before. The current signal looks more like recall fading than starting from zero.",
            "transfer": f"The core idea in {concept_name} is present, but it is not travelling reliably into a new example yet.",
            "fragile_mastery": f"You can handle {concept_name} in familiar settings. IRIS is protecting that progress by checking whether it holds independently.",
            "direct": f"The smallest useful focus is the foundation of {concept_name}; there is no reason to restart the whole subject.",
        }
        return messages.get(cause, f"OVAEL has a bounded working hypothesis for {concept_name}, not a fixed label about you.")

    @staticmethod
    def _falsification(cause: str, concept_name: str) -> str:
        checks = {
            "prerequisite": f"Solve one {concept_name} task after a clean prerequisite check; success without help weakens the prerequisite hypothesis.",
            "misconception": "Explain why the tempting alternative is wrong, then solve a contrast case without a hint.",
            "retention": "Recall the rule after a delay and apply it in one fresh example.",
            "transfer": "Apply the same idea in a different representation or context without copying the prior method.",
            "fragile_mastery": "Complete two spaced, unhinted applications with different surface forms.",
            "assessment_uncertainty": "Repeat the check using a verified item and non-OCR input before attributing the result to the learner.",
            "insufficient_evidence": "Collect one unhinted first attempt with a clear evaluator result.",
            "direct": "Complete one explanation-to-application cycle and then solve an independent example.",
        }
        return checks[cause]

    def _micrograph(self, *, concept_id: str, concept_name: str, state: Any, cause: str, support: float, upstream: list[Any], downstream: list[Any], top_hypothesis: Any) -> dict[str, Any]:
        cause_id = f"cause:{cause}:{concept_id}"
        nodes: list[dict[str, Any]] = [
            {"id": concept_id, "label": concept_name, "role": "focus", "state": state["status"], "value": round(1.0 - float(state["mastery_belief"]), 3)},
            {"id": cause_id, "label": _label(cause), "role": "cause", "state": "hypothesis", "value": round(support, 3)},
        ]
        edges: list[dict[str, Any]] = [{"source": cause_id, "target": concept_id, "relation": "may_explain", "support": round(support, 3)}]
        seen = {concept_id, cause_id}
        if top_hypothesis and top_hypothesis["gap_concept_id"] != concept_id:
            gap_id = str(top_hypothesis["gap_concept_id"])
            nodes.append({"id": gap_id, "label": top_hypothesis["gap_name"] or gap_id, "role": "upstream", "state": "candidate", "value": round(float(top_hypothesis["probability"]), 3)})
            edges.append({"source": gap_id, "target": cause_id, "relation": "supports", "support": round(float(top_hypothesis["probability"]), 3)})
            seen.add(gap_id)
        for row in upstream[:2]:
            cid = str(row["concept_id"])
            if cid in seen:
                continue
            nodes.append({"id": cid, "label": row["name"], "role": "upstream", "state": "dependency", "value": round(float(row["strength"]), 3)})
            edges.append({"source": cid, "target": concept_id, "relation": str(row["relation"]).lower(), "support": round(float(row["strength"]), 3)})
            seen.add(cid)
        for row in downstream[:3]:
            cid = str(row["concept_id"])
            if cid in seen:
                continue
            nodes.append({"id": cid, "label": row["name"], "role": "downstream", "state": "affected", "value": round(float(row["strength"]), 3)})
            edges.append({"source": concept_id, "target": cid, "relation": str(row["relation"]).lower(), "support": round(float(row["strength"]), 3)})
            seen.add(cid)
        return {"nodes": nodes, "edges": edges, "caption": "A bounded causal working graph—not the learner's full knowledge graph."}

    def _reconcile_scope(
        self,
        *,
        user_id: str,
        subject_id: str | None,
        active_case_ids: set[str],
    ) -> None:
        """Hide cases that are no longer supported inside the analyzed scope."""
        clauses = ["user_id=?", "status!='dismissed'"]
        params: list[Any] = [user_id]
        if subject_id:
            clauses.append("subject_id=?")
            params.append(subject_id)
        rows = self.db.fetchall(
            f"SELECT case_id FROM causal_gap_cases WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        stale_ids = [str(row["case_id"]) for row in rows if str(row["case_id"]) not in active_case_ids]
        if not stale_ids:
            return
        now = _now()
        with self.db.transaction() as conn:
            conn.executemany(
                "UPDATE causal_gap_cases SET status='dismissed',updated_at=? WHERE case_id=? AND user_id=?",
                [(now, case_id, user_id) for case_id in stale_ids],
            )

    @staticmethod
    def _next_step(case: dict[str, Any]) -> dict[str, str]:
        cause = str(case["cause_type"])
        if bool(case["abstained"]):
            return {"action": "diagnostic_check", "title": "Collect one clean signal", "detail": case["falsification_check"]}
        mapping = {
            "prerequisite": ("prerequisite_rewind", "Repair the smallest dependency"),
            "misconception": ("contrast", "Contrast the tempting alternative"),
            "retention": ("spaced_recall", "Refresh, then delay the check"),
            "transfer": ("worked_example", "Bridge into a new representation"),
            "fragile_mastery": ("reassess", "Verify with spaced transfer"),
            "direct": ("explain", "Rebuild one foundation"),
        }
        action, title = mapping.get(cause, ("diagnose", "Verify the cause"))
        return {"action": action, "title": title, "detail": case["falsification_check"]}

    @staticmethod
    def _headline(cases: list[dict[str, Any]]) -> str:
        if not cases:
            return "No supported learning gap is ready for attention yet."
        top = cases[0]
        if top.get("abstained"):
            return f"Start with a clean check on {top.get('concept_name', 'the leading concept')}."
        return f"The highest-leverage focus is {top.get('concept_name')}, likely shaped by {str(top.get('cause_label')).lower()}."

    def _persist_case(self, user_id: str, case: dict[str, Any], force_revision: bool = False) -> None:
        now = _now()
        case_id = str(case["case_id"])
        prior = self.db.fetchone(
            """
            SELECT c.current_revision_id,r.snapshot_json,
                   COALESCE((SELECT MAX(revision_no) FROM causal_gap_revisions WHERE case_id=c.case_id),0) AS revision_no
            FROM causal_gap_cases c
            LEFT JOIN causal_gap_revisions r ON r.revision_id=c.current_revision_id
            WHERE c.case_id=? AND c.user_id=?
            """,
            (case_id, user_id),
        )
        snapshot = self.db.dumps(case)
        digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        prior_digest = None
        if prior and prior["snapshot_json"]:
            prior_digest = hashlib.sha256(str(prior["snapshot_json"]).encode("utf-8")).hexdigest()
        if prior and digest == prior_digest and not force_revision:
            return
        revision_id = str(uuid4())
        revision_no = int(prior["revision_no"] if prior else 0) + 1
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO causal_gap_cases(
                    case_id,user_id,subject_id,target_concept_id,status,cause_type,
                    normalized_support,confidence,severity,focus_priority,current_revision_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(case_id) DO UPDATE SET
                    status=excluded.status,cause_type=excluded.cause_type,
                    normalized_support=excluded.normalized_support,confidence=excluded.confidence,
                    severity=excluded.severity,focus_priority=excluded.focus_priority,
                    current_revision_id=excluded.current_revision_id,updated_at=excluded.updated_at
                """,
                (
                    case_id, user_id, case["subject_id"], case["target_concept_id"], case.get("status", "open"),
                    case["cause_type"], case["normalized_support"], case["confidence"], case["severity"],
                    case["focus_priority"], revision_id, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO causal_gap_revisions(revision_id,case_id,revision_no,snapshot_json,created_at) VALUES(?,?,?,?,?)",
                (revision_id, case_id, revision_no, snapshot, now),
            )
