from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .adaptive import AdaptiveEngine
from .learning import LearnerEngine
from .memory import MemoryService
from .models import ModelGateway, ModelUnavailable
from .retrieval import RetrievalService
from .schemas import AdaptiveDecision, AdaptiveState, DiagnosisResult, EvidenceEvent, TutorRequest, TutorResponse


@dataclass(slots=True)
class NavigationPlan:
    subject_id: str
    target_concept_id: str
    requested_action: str
    diagnosis: DiagnosisResult
    learner_capsule: dict[str, Any]
    retrieval_query: str
    adaptive: AdaptiveDecision
    use_supervisor_for_plan: bool = False


class NavigatorAgent:
    """Lead orchestration agent.

    The Navigator decides the narrow state and knowledge scope for the current
    turn. It does not own runtime authority: its structured output is validated
    by application code before any action runs.
    """

    def __init__(
        self,
        learner: LearnerEngine,
        adaptive: AdaptiveEngine,
        memory: MemoryService,
        models: ModelGateway,
    ):
        self.learner = learner
        self.adaptive = adaptive
        self.memory = memory
        self.models = models

    def plan(self, request: TutorRequest) -> NavigationPlan:
        session = self.memory.get_session(request.session_id)
        if not session:
            raise ValueError("Unknown session")
        if session.user_id != request.user_id or session.subject_id != request.subject_id:
            raise ValueError("Tutor request does not match the session")
        if session.status != "active":
            raise ValueError("Tutor request requires an active session")

        concept = self.memory.db.fetchone(
            "SELECT subject_id,name FROM concepts WHERE concept_id=?",
            (request.target_concept_id,),
        )
        if not concept or concept["subject_id"] != request.subject_id:
            raise ValueError("Target concept does not belong to the active subject")

        diagnosis = self.learner.diagnose(request.user_id, request.target_concept_id)
        capsule = self.memory.build_context_capsule(
            user_id=request.user_id,
            subject_id=request.subject_id,
            target_concept_id=request.target_concept_id,
        )
        # Do not send stale persisted hypotheses when we just recomputed a more
        # current diagnosis for this turn.
        capsule["gap_hypotheses"] = [h.model_dump(mode="json") for h in diagnosis.hypotheses[:4]]
        adaptive = self.adaptive.recommend_next(
            user_id=request.user_id,
            subject_id=request.subject_id,
            concept_id=request.target_concept_id,
            diagnosis=diagnosis,
        )

        retrieval_query = request.user_message.strip() or concept["name"]
        use_supervisor = bool(
            self.models.supervisor_available
            and (
                request.force_supervisor
                or request.action in {"worked_example", "prerequisite_rewind"}
                or diagnosis.recommended_action == "reassess"
            )
        )

        # When explicitly configured, the higher model may refine the retrieval
        # query/learning action, but cannot expand memory scope beyond this local
        # capsule or change user/session identity.
        if use_supervisor:
            try:
                refined = self.models.supervisor_json(
                    system=(
                        "You are the planning critic for an adaptive tutor. Use only the supplied "
                        "learner capsule. Return JSON with optional retrieval_query and action. "
                        "Do not invent learner history or change subject/target concept."
                    ),
                    payload={
                        "subject": request.subject_id,
                        "target_concept": request.target_concept_id,
                        "requested_action": request.action,
                        "user_message": request.user_message,
                        "diagnosis": diagnosis.model_dump(mode="json"),
                        "adaptive": adaptive.model_dump(mode="json"),
                        "learner_capsule": capsule,
                    },
                )
                candidate_query = refined.get("retrieval_query")
                if isinstance(candidate_query, str) and 1 <= len(candidate_query) <= 600:
                    retrieval_query = candidate_query.strip()
                candidate_action = refined.get("action")
                allowed = {
                    "explain",
                    "hint",
                    "analogy",
                    "worked_example",
                    "socratic_question",
                    "practice",
                    "reassess",
                    "prerequisite_rewind",
                    "summary",
                }
                if candidate_action in allowed:
                    request.action = candidate_action
            except (ModelUnavailable, ValueError, TypeError):
                pass

        return NavigationPlan(
            subject_id=request.subject_id,
            target_concept_id=request.target_concept_id,
            requested_action=request.action,
            diagnosis=diagnosis,
            learner_capsule=capsule,
            retrieval_query=retrieval_query,
            adaptive=adaptive,
            use_supervisor_for_plan=use_supervisor,
        )


class TutorAgent:
    """API-first pedagogical agent with optional local fallback and gated verification."""

    def __init__(
        self,
        memory: MemoryService,
        retrieval: RetrievalService,
        models: ModelGateway,
    ):
        self.memory = memory
        self.retrieval = retrieval
        self.models = models

    def respond(self, request: TutorRequest, plan: NavigationPlan) -> TutorResponse:
        hypothesis_concepts = [h.concept_id for h in plan.diagnosis.hypotheses[:3]]
        retrieval_concepts = list(
            dict.fromkeys(
                [request.target_concept_id, plan.adaptive.target_concept_id, *hypothesis_concepts]
            )
        )
        hits = self.retrieval.search(
            query=plan.retrieval_query,
            subject_id=request.subject_id,
            concept_ids=retrieval_concepts,
            limit=5,
            verified_only=True,
        )
        citations = [h.chunk_id for h in hits]
        grounded = bool(hits)

        if plan.diagnosis.recommended_action == "ask_diagnostic" and plan.diagnosis.selected_question_id:
            q = self.memory.db.fetchone(
                "SELECT stem FROM questions WHERE question_id=?",
                (plan.diagnosis.selected_question_id,),
            )
            if q and request.action not in {"summary", "worked_example"}:
                message = (
                    "I need one small diagnostic before choosing the explanation. "
                    + q["stem"]
                )
                self.memory.record_intervention(
                    session_id=request.session_id,
                    user_id=request.user_id,
                    subject_id=request.subject_id,
                    concept_id=request.target_concept_id,
                    action_type="diagnostic_question",
                    content=message,
                    grounded=True,
                    source_ids=[],
                    supervisor_used=False,
                    metadata={"question_id": plan.diagnosis.selected_question_id},
                )
                return TutorResponse(
                    message=message,
                    action="diagnostic_question",
                    grounded=True,
                    citations=[],
                    supervisor_used=False,
                    next_check=plan.diagnosis.selected_question_id,
                    diagnosis=plan.diagnosis,
                )

        if request.action == "practice" and plan.adaptive.selected_question_id:
            q = self.memory.db.fetchone(
                "SELECT stem FROM questions WHERE question_id=?",
                (plan.adaptive.selected_question_id,),
            )
            if q:
                message = q["stem"]
                self.memory.record_intervention(
                    session_id=request.session_id,
                    user_id=request.user_id,
                    subject_id=request.subject_id,
                    concept_id=plan.adaptive.target_concept_id,
                    action_type="adaptive_practice",
                    content=message,
                    grounded=True,
                    source_ids=[],
                    supervisor_used=False,
                    metadata={
                        "question_id": plan.adaptive.selected_question_id,
                        "difficulty": plan.adaptive.target_difficulty,
                        "difficulty_band": plan.adaptive.difficulty_band,
                    },
                )
                return TutorResponse(
                    message=message,
                    action="adaptive_practice",
                    grounded=True,
                    citations=[],
                    supervisor_used=False,
                    next_check=plan.adaptive.selected_question_id,
                    diagnosis=plan.diagnosis,
                )

        candidate = self._tutor_or_fallback(request, plan, hits)
        supervisor_used = False
        needs_supervisor = self._needs_supervisor(
            request=request,
            plan=plan,
            grounded=grounded,
            candidate=candidate,
        )
        if needs_supervisor and self.models.supervisor_available:
            revised = self._verify_with_supervisor(request, plan, hits, candidate)
            if revised:
                candidate = revised
            supervisor_used = True

        self.memory.record_intervention(
            session_id=request.session_id,
            user_id=request.user_id,
            subject_id=request.subject_id,
            concept_id=request.target_concept_id,
            action_type=request.action,
            content=candidate,
            grounded=grounded,
            source_ids=[h.source_id for h in hits],
            supervisor_used=supervisor_used,
            metadata={"chunk_ids": citations},
        )
        return TutorResponse(
            message=candidate,
            action=request.action,
            grounded=grounded,
            citations=citations,
            supervisor_used=supervisor_used,
            next_check=self._next_check(plan),
            diagnosis=plan.diagnosis,
        )

    def _tutor_or_fallback(self, request: TutorRequest, plan: NavigationPlan, hits: list[Any]) -> str:
        context = [
            {
                "chunk_id": h.chunk_id,
                "source_id": h.source_id,
                "content": h.content,
            }
            for h in hits
        ]
        system = (
            "You are a concise adaptive tutor. Never invent learner history. Use the supplied "
            "learner state, adaptive decision, response-behavior profile, and trusted context. "
            "Treat behavior as current evidence, not a permanent label. A supported success should "
            "keep some scaffolding; a confident error should trigger a contrastive check; fragile "
            "confidence should not be treated as lack of mastery by itself. Do not expose internal "
            "probabilities or hidden memory. For hints, do not reveal the complete solution. Teach "
            "in small chunks and end with at most one useful check when appropriate. If trusted "
            "context is empty for a factual theory question, say that you lack verified material "
            "rather than guessing."
        )
        payload = {
            "action": request.action,
            "subject": request.subject_id,
            "target_concept": request.target_concept_id,
            "learner_capsule": plan.learner_capsule,
            "diagnosis": plan.diagnosis.model_dump(mode="json"),
            "adaptive": plan.adaptive.model_dump(mode="json"),
            "user_message": request.user_message,
            "trusted_context": context,
        }
        body = json.dumps(payload, ensure_ascii=False)
        try:
            return self.models.tutor_chat(
                system=system, user=body, temperature=0.2, max_tokens=650
            ).text
        except ModelUnavailable:
            pass
        # Optional local model is a deployment/cost optimization, not a core
        # dependency. It can be enabled without changing the tutoring contract.
        try:
            return self.models.local_chat(
                system=system, user=body, temperature=0.2, max_tokens=650
            ).text
        except ModelUnavailable:
            return self._fallback(request, plan, hits)

    def _fallback(self, request: TutorRequest, plan: NavigationPlan, hits: list[Any]) -> str:
        if request.action == "hint":
            if request.subject_id == "DSA":
                return (
                    "Focus on the invariant that must remain true after every step. "
                    "Check whether your update always makes the active search/problem state strictly smaller."
                )
            return "Focus on the distinguishing feature of the concept, then compare it with the nearest confusing concept."
        if hits:
            excerpt = hits[0].content.strip()
            if len(excerpt) > 500:
                excerpt = excerpt[:497].rstrip() + "..."
            return excerpt
        if request.subject_id == "DSA":
            return (
                "I do not have enough verified curriculum context for a full explanation. "
                "I can still use executable/static evidence to ask a diagnostic or give a bounded hint."
            )
        return (
            "I do not have verified material for this theory response yet. "
            "Add the relevant trusted source to the knowledge corpus before teaching this fact."
        )

    def _needs_supervisor(
        self,
        *,
        request: TutorRequest,
        plan: NavigationPlan,
        grounded: bool,
        candidate: str,
    ) -> bool:
        if request.force_supervisor:
            return True
        if request.subject_id != "DSA" and not grounded:
            return True
        if plan.diagnosis.recommended_action in {"reassess", "insufficient_evidence"}:
            return True
        if len(candidate) > 3000:
            return True
        return False

    def _verify_with_supervisor(
        self,
        request: TutorRequest,
        plan: NavigationPlan,
        hits: list[Any],
        candidate: str,
    ) -> str | None:
        payload = {
            "task": "verify_tutor_response",
            "action": request.action,
            "student_message": request.user_message,
            "learner_capsule": plan.learner_capsule,
            "candidate_response": candidate,
            "trusted_context": [h.content for h in hits],
            "rules": {
                "do_not_invent_history": True,
                "theory_claims_must_be_grounded": request.subject_id != "DSA",
                "hint_must_not_leak_full_solution": request.action == "hint",
            },
            "return": {"decision": "PASS|REVISE|BLOCK", "revised_response": "optional"},
        }
        try:
            result = self.models.supervisor_json(
                system=(
                    "Verify pedagogical appropriateness and grounding using only supplied context. "
                    "Return JSON. PASS keeps the candidate. REVISE must include revised_response. "
                    "BLOCK should return a short safe replacement in revised_response."
                ),
                payload=payload,
            )
        except (ModelUnavailable, ValueError, TypeError):
            return None
        decision = str(result.get("decision", "PASS")).upper()
        revised = result.get("revised_response")
        if decision in {"REVISE", "BLOCK"} and isinstance(revised, str) and revised.strip():
            return revised.strip()
        return None

    @staticmethod
    def _next_check(plan: NavigationPlan) -> str | None:
        if plan.diagnosis.selected_question_id:
            return plan.diagnosis.selected_question_id
        if plan.adaptive.selected_question_id:
            return plan.adaptive.selected_question_id
        if plan.diagnosis.hypotheses:
            return f"reassess:{plan.diagnosis.hypotheses[0].concept_id}"
        return None


class MemoryCuratorAgent:
    """Single logical writer for learner events and state transitions."""

    def __init__(self, learner: LearnerEngine, adaptive: AdaptiveEngine, memory: MemoryService):
        self.learner = learner
        self.adaptive = adaptive
        self.memory = memory

    def record(self, event: EvidenceEvent) -> tuple[list[Any], DiagnosisResult, AdaptiveState]:
        # Learner-state and adaptive-state are a single logical observation. Compute
        # from the same pre-event snapshot and commit atomically.
        with self.memory.write_lock:
            states, diagnosis = self.learner.compute_event_update(event)
            adaptive_state = self.adaptive.update_from_event(event, persist=False)
            if self.memory.personalization_enabled(event.user_id):
                self.memory.commit_learning_update(
                    event=event, states=states, hypotheses=diagnosis.hypotheses,
                    target_concept_id=event.primary_concept_id, adaptive_state=adaptive_state,
                )
            return states, diagnosis, adaptive_state

    def close_session(self, session_id: str) -> dict[str, Any]:
        return self.memory.complete_session(session_id)

    def rebuild_subject_after_curriculum_update(self, subject_id: str) -> int:
        """Recompute derived learner state after trusted curriculum parameters change.

        Subject packs can change BKT parameters or graph structure. Keeping old
        derived state after such a change would mix two model definitions. Raw
        events remain the source of truth, so replay them under the new pack and
        invalidate cached gap hypotheses.
        """
        with self.memory.write_lock:
            concept_ids = {
                row["concept_id"]
                for row in self.memory.db.fetchall(
                    "SELECT concept_id FROM concepts WHERE subject_id=?", (subject_id,)
                )
            }
            if not concept_ids:
                return 0
            users = {
                row["user_id"]
                for row in self.memory.db.fetchall(
                    "SELECT DISTINCT user_id FROM learning_events WHERE subject_id=?",
                    (subject_id,),
                )
            }
            # Gap probabilities depend on both learner state and graph topology.
            # They are cheap to recompute lazily at the next diagnosis.
            with self.memory.db.transaction() as conn:
                conn.execute(
                    """
                    DELETE FROM gap_hypotheses
                    WHERE target_concept_id IN (SELECT concept_id FROM concepts WHERE subject_id=?)
                       OR gap_concept_id IN (SELECT concept_id FROM concepts WHERE subject_id=?)
                    """,
                    (subject_id, subject_id),
                )
            for user_id in sorted(users):
                self.learner.rebuild_user_state(user_id, concept_ids)
                self.adaptive.rebuild_user_state(user_id, concept_ids)
            return len(users)

    def delete_and_rebuild(
        self,
        *,
        user_id: str,
        subject_id: str | None,
        start_at: Any,
        end_at: Any,
        delete_all: bool,
    ) -> set[str]:
        affected = self.memory.delete_memory(
            user_id=user_id,
            subject_id=subject_id,
            start_at=start_at,
            end_at=end_at,
            delete_all=delete_all,
        )

        # OVAEL teaching/session artifacts are a second representation of learner
        # interaction history. Privacy deletion must remove them too; otherwise a
        # "delete all learning memory" request would leave prompts, responses and
        # external-session evidence behind after the legacy ledger was cleared.
        clauses = ["owner_user_id=?"]
        params: list[Any] = [user_id]
        if subject_id:
            clauses.append("subject_id=?"); params.append(subject_id)
        if start_at:
            clauses.append("updated_at>=?"); params.append(start_at.isoformat())
        if end_at:
            clauses.append("created_at<=?"); params.append(end_at.isoformat())
        if delete_all:
            clauses = ["owner_user_id=?"]; params = [user_id]
        session_rows = self.memory.db.fetchall(
            f"SELECT teaching_session_id,state_json FROM teaching_sessions WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        teaching_ids = [r["teaching_session_id"] for r in session_rows]
        shadow_user_ids: set[str] = set()
        for r in session_rows:
            state = self.memory.db.loads(r["state_json"], {})
            engine_id = str(state.get("engine_user_id") or "")
            if engine_id.startswith(f"lf:{user_id}:"):
                shadow_user_ids.add(engine_id)

        with self.memory.write_lock, self.memory.db.transaction() as conn:
            for shadow_id in shadow_user_ids:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM concept_state WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM adaptive_state WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM gap_hypotheses WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM compacted_history WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM privacy_settings WHERE user_id=?", (shadow_id,))
            if teaching_ids:
                for start in range(0, len(teaching_ids), 300):
                    batch = teaching_ids[start:start + 300]
                    placeholders = ",".join("?" for _ in batch)
                    conn.execute(f"DELETE FROM handoff_tokens WHERE user_id=? AND teaching_session_id IN ({placeholders})", (user_id, *batch))
                    conn.execute(f"DELETE FROM external_learning_inbox WHERE user_id=? AND external_session_id IN ({placeholders})", (user_id, *batch))
                    conn.execute(f"DELETE FROM teaching_sessions WHERE owner_user_id=? AND teaching_session_id IN ({placeholders})", (user_id, *batch))

            item_clauses = ["owner_user_id=?"]
            item_params: list[Any] = [user_id]
            if subject_id:
                item_clauses.append("subject_id=?"); item_params.append(subject_id)
            if start_at:
                item_clauses.append("created_at>=?"); item_params.append(start_at.isoformat())
            if end_at:
                item_clauses.append("created_at<=?"); item_params.append(end_at.isoformat())
            conn.execute(f"DELETE FROM generated_items WHERE {' AND '.join(item_clauses)}", tuple(item_params))

            # A relay capsule can contain a mixture of concepts and is intentionally
            # denormalized/short-lived. Clear it on any privacy deletion so stale
            # deleted information cannot continue flowing through MCP.
            conn.execute("DELETE FROM mcp_context_relay WHERE user_id=?", (user_id,))
            if delete_all:
                conn.execute("DELETE FROM external_learning_inbox WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM handoff_tokens WHERE user_id=?", (user_id,))

        if not delete_all and affected:
            self.learner.rebuild_user_state(user_id, affected)
            self.adaptive.rebuild_user_state(user_id, affected)
        return affected

# ---------------------------------------------------------------------------
# OVAEL v0.01 named lead-agent layer
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# OVAEL v0.01 named lead-agent layer
# ---------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor
from dataclasses import field
from threading import Lock


@dataclass(slots=True)
class LeadFinding:
    lead: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    specialists: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class OvaelPlan:
    target_concept_id: str
    target_difficulty: float
    action: str
    learner_reason: str
    sam: LeadFinding
    carl: LeadFinding
    trav: LeadFinding
    x_specialists: list[dict[str, Any]]
    x_summary: str


class SpecialistBudget:
    """Hard cap/depth enforcement for ephemeral specialists.

    Each lead has its own <=3 quota and all leads share a configurable global
    budget. Children never receive a budget object, so recursive spawning is not
    representable through this layer.
    """

    def __init__(self, max_per_lead: int = 3, max_total: int = 12):
        self.max_per_lead = max(0, min(3, max_per_lead))
        self.max_total = max(0, min(12, max_total))
        self.used_by_lead: dict[str, int] = {}
        self._legacy_used = 0
        self._lock = Lock()

    @property
    def used(self) -> int:
        return sum(self.used_by_lead.values()) + self._legacy_used

    def grant(self, lead: str | int, requested: int | None = None) -> int:
        # Backward-compatible one-argument form preserves the old global-budget
        # helper used by v0.01 tests. New agent code always supplies a lead name,
        # which enforces the independent <=3 quota required by OVAEL.
        with self._lock:
            if requested is None:
                requested = int(lead)
                total_left = max(0, self.max_total - self.used)
                allowed = min(max(0, requested), self.max_per_lead, total_left)
                self._legacy_used += allowed
                return allowed
            lead_name = str(lead)
            already = self.used_by_lead.get(lead_name, 0)
            per_lead_left = max(0, self.max_per_lead - already)
            total_left = max(0, self.max_total - self.used)
            allowed = min(max(0, requested), per_lead_left, total_left)
            self.used_by_lead[lead_name] = already + allowed
            return allowed


class _ParallelSpecialists:
    @staticmethod
    def run(jobs: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        if not jobs:
            return []
        with ThreadPoolExecutor(max_workers=min(3, len(jobs))) as pool:
            futures = [(name, pool.submit(fn)) for name, fn in jobs]
            results: list[dict[str, Any]] = []
            for name, future in futures:
                try:
                    payload = future.result()
                except Exception as exc:  # specialist failure must not kill the lead
                    payload = {"status": "failed", "error_type": type(exc).__name__}
                results.append({"specialist": name, **payload})
            return results


class SamAgent:
    """Learner-intelligence lead: evidence, gaps, recurrence and uncertainty."""

    name = "Sam"
    skills = (
        "learner_state",
        "gap_detection",
        "prerequisite_analysis",
        "misconception_analysis",
        "adaptive_uncertainty",
    )

    def __init__(self, learner: LearnerEngine, adaptive: AdaptiveEngine, memory: MemoryService):
        self.learner = learner
        self.adaptive = adaptive
        self.memory = memory

    def analyze(self, *, user_id: str, subject_id: str, concept_id: str, budget: SpecialistBudget) -> LeadFinding:
        diagnosis = self.learner.diagnose(user_id, concept_id)
        decision = self.adaptive.recommend_next(
            user_id=user_id, subject_id=subject_id, concept_id=concept_id, diagnosis=diagnosis
        )
        requested = 0
        if not diagnosis.confident:
            requested += 1
        if diagnosis.entropy > 0.8:
            requested += 1
        if len(diagnosis.hypotheses) >= 2 and diagnosis.confidence_margin < 0.18:
            requested += 1
        count = budget.grant(self.name, min(3, requested))
        jobs = [
            ("misconception_analyst", lambda: self._misconception_specialist(diagnosis)),
            ("prerequisite_analyst", lambda: self._prerequisite_specialist(diagnosis)),
            ("difficulty_analyst", lambda: self._difficulty_specialist(decision)),
        ][:count]
        specialists = _ParallelSpecialists.run(jobs)
        top = diagnosis.hypotheses[0] if diagnosis.hypotheses else None
        reason = (
            f"Likely blocker: {top.concept_id}"
            if top and diagnosis.confident
            else "Learner state needs another check"
        )
        return LeadFinding(
            lead=self.name,
            summary=reason,
            data={
                "diagnosis": diagnosis.model_dump(mode="json"),
                "adaptive": decision.model_dump(mode="json"),
            },
            specialists=specialists,
        )

    @staticmethod
    def _misconception_specialist(diagnosis: DiagnosisResult) -> dict[str, Any]:
        items = [h for h in diagnosis.hypotheses if h.source == "misconception"][:3]
        return {"status": "ok", "hypotheses": [h.model_dump(mode="json") for h in items]}

    @staticmethod
    def _prerequisite_specialist(diagnosis: DiagnosisResult) -> dict[str, Any]:
        items = [h for h in diagnosis.hypotheses if h.source in {"prerequisite", "component"}][:3]
        return {"status": "ok", "hypotheses": [h.model_dump(mode="json") for h in items]}

    @staticmethod
    def _difficulty_specialist(decision: AdaptiveDecision) -> dict[str, Any]:
        return {
            "status": "ok",
            "target_difficulty": decision.target_difficulty,
            "uncertainty": decision.uncertainty,
            "expected_success": decision.expected_success,
        }


class CarlAgent:
    """Teaching-architecture lead: decides how to teach, never mastery."""

    name = "Carl"
    skills = (
        "pedagogy_policy",
        "socratic_teaching",
        "contrastive_explanation",
        "worked_examples",
        "dynamic_assessment_specification",
    )

    def decide(self, sam: LeadFinding, *, requested_mode: str, budget: SpecialistBudget) -> LeadFinding:
        diagnosis = sam.data["diagnosis"]
        adaptive = sam.data["adaptive"]
        action = "practice"
        learner_reason = "The current concept is ready for another targeted check."
        recommended = diagnosis.get("recommended_action")
        hypotheses = diagnosis.get("hypotheses") or []
        if recommended == "ask_diagnostic":
            action = "diagnose"
            learner_reason = "Checking one prerequisite before changing the challenge."
        elif recommended == "teach":
            action = "explain"
            learner_reason = "One earlier idea looks unstable, so the next move is an explanation."
        elif recommended == "reassess":
            action = "reassess"
            learner_reason = "The concept improved and now needs a fresh verification."
        elif requested_mode == "review":
            action = "reassess"
            learner_reason = "Review mode will verify what still holds without restarting the topic."

        requested = 0
        if not diagnosis.get("confident"):
            requested += 1
        if len(hypotheses) > 1:
            requested += 1
        if action in {"explain", "diagnose", "prerequisite_rewind"} and requested_mode != "calibration":
            requested += 1
        count = budget.grant(self.name, min(3, requested))
        jobs = [
            (
                "pedagogy_critic",
                lambda: {
                    "status": "ok",
                    "recommended_action": action,
                    "constraint": "Use the least intrusive move that can resolve current uncertainty.",
                },
            ),
            (
                "transfer_designer",
                lambda: {
                    "status": "ok",
                    "goal": "Prefer transfer/application evidence over repeated recall when appropriate.",
                },
            ),
            (
                "scaffolding_critic",
                lambda: {
                    "status": "ok",
                    "goal": "Do not lower challenge merely because a prerequisite check is needed.",
                },
            ),
        ][:count]
        specialists = _ParallelSpecialists.run(jobs)
        return LeadFinding(
            lead=self.name,
            summary=action,
            data={"action": action, "learner_reason": learner_reason, "adaptive": adaptive},
            specialists=specialists,
        )


class TravAgent:
    """Knowledge/retrieval lead. Retrieved content is always treated as data."""

    name = "Trav"
    skills = (
        "hybrid_retrieval",
        "course_scoping",
        "source_provenance",
        "document_context",
        "tool_selection",
    )

    def __init__(self, retrieval: RetrievalService):
        self.retrieval = retrieval

    def gather(
        self,
        *,
        user_id: str,
        subject_id: str,
        concept_id: str,
        query: str,
        budget: SpecialistBudget,
        course_id: str | None = None,
    ) -> LeadFinding:
        hits = self.retrieval.search(
            query=query or concept_id,
            subject_id=subject_id,
            concept_ids=[concept_id],
            limit=6,
            verified_only=True,
            user_id=user_id,
            course_id=course_id,
        )
        if not hits:
            hits = self.retrieval.search(
                query=query or concept_id,
                subject_id=subject_id,
                concept_ids=[],
                limit=6,
                verified_only=True,
                user_id=user_id,
                course_id=course_id,
            )

        requested = 0
        if not hits:
            requested = 2
        elif any(not h.verified for h in hits):
            requested += 1
        if len({h.source_id for h in hits}) > 3:
            requested += 1
        count = budget.grant(self.name, min(3, requested))
        jobs = [
            (
                "source_coverage_critic",
                lambda: {
                    "status": "ok",
                    "finding": "No source coverage" if not hits else "Source coverage available",
                },
            ),
            (
                "provenance_critic",
                lambda: {
                    "status": "ok",
                    "private_unverified_present": any(not h.verified for h in hits),
                },
            ),
            (
                "context_budget_critic",
                lambda: {
                    "status": "ok",
                    "hit_count": len(hits),
                    "instruction": "Use only the smallest source subset needed for the teaching move.",
                },
            ),
        ][:count]
        specialists = _ParallelSpecialists.run(jobs)
        return LeadFinding(
            lead=self.name,
            summary=f"{len(hits)} authorized source hits",
            data={"hits": [h.model_dump(mode="json") for h in hits]},
            specialists=specialists,
        )


class XAgent:
    """Cross-lead orchestrator/critic with hard specialist limits."""

    name = "X"
    skills = (
        "routing",
        "conflict_resolution",
        "specialist_spawn",
        "cross_domain_quality_gate",
        "scope_safety",
    )

    def __init__(
        self,
        sam: SamAgent,
        carl: CarlAgent,
        trav: TravAgent,
        *,
        max_total_specialists: int = 12,
    ):
        self.sam = sam
        self.carl = carl
        self.trav = trav
        self.max_total_specialists = max(0, min(12, max_total_specialists))

    def plan(
        self,
        *,
        user_id: str,
        subject_id: str,
        concept_id: str,
        requested_mode: str,
        query: str,
        course_id: str | None = None,
        content_user_id: str | None = None,
    ) -> OvaelPlan:
        budget = SpecialistBudget(max_per_lead=3, max_total=self.max_total_specialists)
        sam = self.sam.analyze(
            user_id=user_id, subject_id=subject_id, concept_id=concept_id, budget=budget
        )
        carl = self.carl.decide(sam, requested_mode=requested_mode, budget=budget)
        target = sam.data["adaptive"]["target_concept_id"]
        trav = self.trav.gather(
            user_id=content_user_id or user_id,
            subject_id=subject_id,
            concept_id=target,
            query=query or target,
            budget=budget,
            course_id=course_id,
        )
        difficulty = float(sam.data["adaptive"]["target_difficulty"])
        action = str(carl.data["action"])
        reason = str(carl.data["learner_reason"])

        x_requested = 0
        if sam.specialists and carl.specialists:
            x_requested += 1
        if not trav.data.get("hits"):
            x_requested += 1
        if action in {"diagnose", "prerequisite_rewind"}:
            x_requested += 1
        x_count = budget.grant(self.name, min(3, x_requested))
        x_jobs = [
            (
                "cross_lead_consistency_critic",
                lambda: {
                    "status": "ok",
                    "action": action,
                    "target_concept_id": target,
                    "difficulty": difficulty,
                },
            ),
            (
                "scope_safety_critic",
                lambda: {
                    "status": "ok",
                    "course_scoped": bool(course_id),
                    "source_hits": len(trav.data.get("hits") or []),
                },
            ),
            (
                "final_quality_critic",
                lambda: {
                    "status": "ok",
                    "instruction": "Preserve learner-state authority in deterministic evaluators; model output is advisory/generative.",
                },
            ),
        ][:x_count]
        x_specialists = _ParallelSpecialists.run(x_jobs)

        return OvaelPlan(
            target_concept_id=target,
            target_difficulty=difficulty,
            action=action,
            learner_reason=reason,
            sam=sam,
            carl=carl,
            trav=trav,
            x_specialists=x_specialists,
            x_summary=(
                f"X accepted {action} at difficulty {difficulty:.3f}; "
                f"specialists used={budget.used}/{budget.max_total}."
            ),
        )
