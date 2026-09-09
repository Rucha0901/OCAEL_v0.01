from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import Database
from .memory import MemoryService
from .schemas import (
    ConceptState,
    DiagnosisResult,
    EvidenceEvent,
    GapHypothesis,
    QuestionRecord,
    ResponseBehavior,
)


@dataclass(slots=True, frozen=True)
class BKTParams:
    prior: float = 0.20
    slip: float = 0.10
    guess: float = 0.20
    learn: float = 0.15

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "BKTParams":
        value = value or {}
        defaults = cls()
        params = cls(
            prior=float(value.get("prior", defaults.prior)),
            slip=float(value.get("slip", defaults.slip)),
            guess=float(value.get("guess", defaults.guess)),
            learn=float(value.get("learn", defaults.learn)),
        )
        for name in ("prior", "slip", "guess", "learn"):
            v = getattr(params, name)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"BKT parameter {name} must be in [0,1]")
        if params.slip + params.guess >= 1.0:
            raise ValueError("BKT slip + guess must be < 1 so observations remain informative")
        return params


class LearnerEngine:
    """Deterministic learner-state updater and diagnostic engine.

    BKT is the interpretable baseline for concept-level state. Gap diagnosis is
    kept separate: it forms a normalized hypothesis distribution over direct,
    prerequisite, and misconception candidates, then uses information gain to
    select a diagnostic item when the distribution is ambiguous.
    """

    # These are product calibration thresholds, not universal educational laws.
    VERIFIED_MASTERY = 0.85
    RECOVERED_MASTERY = 0.75
    BLOCKER_MASTERY = 0.35
    REVIEW_MASTERY = 0.55
    MIN_STATUS_CONFIDENCE = 0.60
    DIAGNOSIS_TOP_THRESHOLD = 0.62
    DIAGNOSIS_MARGIN_THRESHOLD = 0.15

    def __init__(self, db: Database, memory: MemoryService):
        self.db = db
        self.memory = memory

    def validate_event(self, event: EvidenceEvent) -> None:
        # Event IDs are durable ledger identities. A duplicate must be rejected
        # before state is recomputed, otherwise a client retry can become a 500
        # from SQLite or accidentally be interpreted as another observation.
        if self.db.fetchone("SELECT 1 FROM learning_events WHERE event_id=?", (event.event_id,)):
            raise ValueError("Evidence event_id has already been recorded")

        session = self.memory.get_session(event.session_id)
        if not session:
            raise ValueError("Unknown learner session")
        if session.status != "active":
            raise ValueError("Cannot record evidence into a completed session")
        if session.user_id != event.user_id or session.subject_id != event.subject_id:
            raise ValueError("Evidence identity does not match the learner session")

        # Prevent client clock errors/future timestamps from poisoning recency,
        # compaction and date-range deletion. Small skew is tolerated.
        now = datetime.now(timezone.utc)
        skew = timedelta(minutes=5)
        if event.timestamp > now + skew:
            raise ValueError("Evidence timestamp is too far in the future")
        if event.timestamp < session.started_at - skew:
            raise ValueError("Evidence timestamp predates the learner session")

        for concept_id in event.concept_ids:
            row = self.db.fetchone(
                "SELECT subject_id FROM concepts WHERE concept_id=?", (concept_id,)
            )
            if not row:
                raise ValueError(f"Unknown concept_id: {concept_id}")
            if row["subject_id"] != event.subject_id:
                raise ValueError(
                    f"Concept {concept_id!r} does not belong to subject {event.subject_id!r}"
                )

    # ------------------------------------------------------------------
    # Curriculum graph
    # ------------------------------------------------------------------
    def prerequisites(self, concept_id: str, max_depth: int = 3) -> list[tuple[str, float, int]]:
        """Return prerequisite ancestors as (concept_id, path_strength, depth).

        Edge semantics: source PREREQUISITE_OF target.
        Path strength is the product of explicit curriculum edge strengths.
        """
        seen: dict[str, tuple[float, int]] = {}
        frontier: list[tuple[str, float, int]] = [(concept_id, 1.0, 0)]
        while frontier:
            current, path_strength, depth = frontier.pop(0)
            if depth >= max_depth:
                continue
            rows = self.db.fetchall(
                """
                SELECT source_concept_id,strength FROM concept_edges
                WHERE target_concept_id=? AND relation='PREREQUISITE_OF'
                ORDER BY strength DESC
                """,
                (current,),
            )
            for row in rows:
                source = row["source_concept_id"]
                strength = path_strength * float(row["strength"])
                next_depth = depth + 1
                previous = seen.get(source)
                if previous is None or strength > previous[0]:
                    seen[source] = (strength, next_depth)
                    frontier.append((source, strength, next_depth))
        return [(cid, strength, depth) for cid, (strength, depth) in seen.items()]

    def misconception_neighbors(self, concept_id: str) -> list[str]:
        rows = self.db.fetchall(
            """
            SELECT source_concept_id,target_concept_id FROM concept_edges
            WHERE relation='COMMONLY_CONFUSED_WITH'
              AND (source_concept_id=? OR target_concept_id=?)
            """,
            (concept_id, concept_id),
        )
        result: list[str] = []
        for row in rows:
            other = (
                row["target_concept_id"]
                if row["source_concept_id"] == concept_id
                else row["source_concept_id"]
            )
            if other not in result:
                result.append(other)
        return result

    def component_candidates(self, concept_id: str) -> list[tuple[str, float]]:
        """Return explicit subskills/components represented as source PART_OF target."""
        rows = self.db.fetchall(
            """
            SELECT source_concept_id,strength FROM concept_edges
            WHERE target_concept_id=? AND relation='PART_OF'
            ORDER BY strength DESC
            """,
            (concept_id,),
        )
        return [(r["source_concept_id"], float(r["strength"])) for r in rows]

    # ------------------------------------------------------------------
    # BKT learner state
    # ------------------------------------------------------------------
    def _params(self, concept_id: str) -> BKTParams:
        row = self.db.fetchone("SELECT bkt_params_json FROM concepts WHERE concept_id=?", (concept_id,))
        if not row:
            raise ValueError(f"Unknown concept_id: {concept_id}")
        return BKTParams.from_mapping(self.db.loads(row["bkt_params_json"], {}))

    @staticmethod
    def _posterior(prior: float, correct: bool, params: BKTParams) -> float:
        eps = 1e-12
        if correct:
            num = prior * (1.0 - params.slip)
            den = num + (1.0 - prior) * params.guess
        else:
            num = prior * params.slip
            den = num + (1.0 - prior) * (1.0 - params.guess)
        return num / max(den, eps)

    @staticmethod
    def _learning_transition(posterior: float, learn: float) -> float:
        return posterior + (1.0 - posterior) * learn

    @staticmethod
    def _evidence_confidence(evidence_count: int) -> float:
        # Evidence-sufficiency indicator, not a probability of being correct.
        # Four independent observations ~= 0.5; more evidence approaches 1.
        if evidence_count <= 0:
            return 0.0
        return 1.0 - 2.0 ** (-evidence_count / 4.0)

    def _status(self, old_status: str, mastery: float, confidence: float) -> str:
        if confidence < 0.25:
            return "unknown"
        if old_status == "active_blocker" and mastery >= self.RECOVERED_MASTERY:
            return "recovered"
        if confidence >= self.MIN_STATUS_CONFIDENCE and mastery >= self.VERIFIED_MASTERY:
            return "verified"
        if confidence >= self.MIN_STATUS_CONFIDENCE and mastery <= self.BLOCKER_MASTERY:
            return "active_blocker"
        if mastery < self.REVIEW_MASTERY:
            return "needs_review"
        return "developing"

    @staticmethod
    def _mastery_evidence_weight(event: EvidenceEvent, base_strength: float) -> float:
        """Apply support/retry confidence to mastery evidence, not correctness.

        A correct response after hints remains correct, but it is weaker evidence
        of independent mastery. Response speed and self-reported confidence are
        deliberately excluded from mastery probability updates.
        """
        weight = max(0.0, min(1.0, base_strength))
        if weight <= 0.0:
            return 0.0
        raw = event.metadata.get("behavior") or {}
        try:
            behavior = ResponseBehavior.model_validate(raw if isinstance(raw, dict) else {})
        except ValueError:
            behavior = ResponseBehavior()
        if behavior.hint_count > 0:
            weight /= 1.0 + 0.35 * behavior.hint_count
        if behavior.attempt_count > 1:
            weight /= 1.0 + 0.15 * (behavior.attempt_count - 1)
        if behavior.evaluator_confidence is not None:
            weight *= max(0.1, min(1.0, behavior.evaluator_confidence))
        return max(0.0, min(1.0, weight))

    def update_states_for_event(self, event: EvidenceEvent) -> list[ConceptState]:
        raw_strengths = event.metadata.get("concept_strengths", {})
        concept_strengths = raw_strengths if isinstance(raw_strengths, dict) else {}
        result: list[ConceptState] = []
        use_history = self.memory.personalization_enabled(event.user_id)

        for concept_id in event.concept_ids:
            params = self._params(concept_id)
            previous = self.memory.get_concept_state(event.user_id, concept_id) if use_history else None
            prior = previous.mastery_belief if previous else params.prior
            old_status = previous.status if previous else "unknown"
            old_count = previous.evidence_count if previous else 0
            recurrence = previous.recurrence_count if previous else 0

            base_strength = event.evidence_strength
            try:
                multiplier = float(
                    concept_strengths.get(
                        concept_id, 1.0 if concept_id == event.primary_concept_id else 0.35
                    )
                )
            except (TypeError, ValueError):
                multiplier = 1.0 if concept_id == event.primary_concept_id else 0.35
            base_strength *= multiplier
            strength = self._mastery_evidence_weight(event, base_strength)
            # BKT confidence is confidence in *mastery observations*, not in the
            # fact that some interaction occurred. Static suspicion, OCR input or
            # an ungraded free-text response may still belong in the event ledger
            # and current diagnosis, but cannot make mastery more certain without
            # an evaluated binary outcome.
            observed = event.correct
            if strength <= 0.0 or observed is None:
                if previous is not None:
                    result.append(previous)
                continue

            mastery = prior
            exact_post = self._posterior(prior, observed, params)
            # Weak/secondary evidence moves only part way toward the exact BKT posterior.
            weighted_post = prior + strength * (exact_post - prior)
            mastery = self._learning_transition(weighted_post, params.learn * strength)

            new_count = old_count + 1
            confidence = self._evidence_confidence(new_count)
            if event.mistake_type and strength > 0 and (event.correct is False or (event.score is not None and event.score < 0.5)):
                recurrence += 1
            status = self._status(old_status, mastery, confidence)

            result.append(
                ConceptState(
                    user_id=event.user_id,
                    concept_id=concept_id,
                    mastery_belief=max(0.0, min(1.0, mastery)),
                    confidence=confidence,
                    evidence_count=new_count,
                    last_observed_at=event.timestamp,
                    status=status,
                    recurrence_count=recurrence,
                )
            )
        return result

    def rebuild_user_state(self, user_id: str, concept_ids: set[str] | None = None) -> None:
        """Replay retained events after a privacy deletion or repair.

        The replay is intentionally deterministic. It does not rewrite the event
        ledger. It only rebuilds concept_state for the selected concepts.
        """
        if concept_ids:
            values = list(concept_ids)
            with self.db.transaction() as conn:
                for start in range(0, len(values), 400):
                    batch = values[start:start + 400]
                    placeholders = ",".join("?" for _ in batch)
                    conn.execute(
                        f"DELETE FROM concept_state WHERE user_id=? AND concept_id IN ({placeholders})",
                        (user_id, *batch),
                    )
        else:
            self.db.execute("DELETE FROM concept_state WHERE user_id=?", (user_id,))

        rows = self.db.fetchall(
            "SELECT * FROM learning_events WHERE user_id=? ORDER BY timestamp,event_id",
            (user_id,),
        )
        in_memory: dict[str, ConceptState] = {}
        for row in rows:
            event_concepts = self.db.loads(row["concept_ids_json"], [])
            if concept_ids is not None and not (set(event_concepts) & concept_ids):
                continue
            event = EvidenceEvent(
                event_id=row["event_id"],
                session_id=row["session_id"],
                user_id=row["user_id"],
                subject_id=row["subject_id"],
                concept_ids=event_concepts,
                primary_concept_id=row["primary_concept_id"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                activity_type=row["activity_type"],
                input_mode=row["input_mode"],
                correct=None if row["correct"] is None else bool(row["correct"]),
                score=row["score"],
                evidence_strength=row["evidence_strength"],
                response_text=row["response_text"],
                mistake_type=row["mistake_type"],
                concept_likelihoods=self.db.loads(row["concept_likelihoods_json"], {}),
                metadata=self.db.loads(row["metadata_json"], {}),
            )
            for cid in event.concept_ids:
                if concept_ids is not None and cid not in concept_ids:
                    continue
                params = self._params(cid)
                previous = in_memory.get(cid)
                prior = previous.mastery_belief if previous else params.prior
                count = previous.evidence_count if previous else 0
                recurrence = previous.recurrence_count if previous else 0
                old_status = previous.status if previous else "unknown"
                raw_strengths = event.metadata.get("concept_strengths", {})
                strengths = raw_strengths if isinstance(raw_strengths, dict) else {}
                try:
                    multiplier = float(strengths.get(cid, 1.0 if cid == event.primary_concept_id else 0.35))
                except (TypeError, ValueError):
                    multiplier = 1.0 if cid == event.primary_concept_id else 0.35
                strength = self._mastery_evidence_weight(
                    event, event.evidence_strength * multiplier
                )
                if strength <= 0.0 or event.correct is None:
                    # Non-evaluated events are auditable but are not mastery
                    # observations. Preserve any earlier in-memory state exactly.
                    continue
                mastery = prior
                post = self._posterior(prior, event.correct, params)
                post = prior + strength * (post - prior)
                mastery = self._learning_transition(post, params.learn * strength)
                count += 1
                if event.mistake_type and strength > 0 and (
                    event.correct is False or (event.score is not None and event.score < 0.5)
                ):
                    recurrence += 1
                confidence = self._evidence_confidence(count)
                state = ConceptState(
                    user_id=user_id,
                    concept_id=cid,
                    mastery_belief=mastery,
                    confidence=confidence,
                    evidence_count=count,
                    last_observed_at=event.timestamp,
                    status=self._status(old_status, mastery, confidence),
                    recurrence_count=recurrence,
                )
                in_memory[cid] = state

        if not in_memory:
            return
        with self.db.transaction() as conn:
            now = datetime.now(timezone.utc).isoformat()
            for state in in_memory.values():
                conn.execute(
                    """
                    INSERT INTO concept_state(
                      user_id,concept_id,mastery_belief,confidence,evidence_count,
                      last_observed_at,status,recurrence_count,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,concept_id) DO UPDATE SET
                      mastery_belief=excluded.mastery_belief,
                      confidence=excluded.confidence,
                      evidence_count=excluded.evidence_count,
                      last_observed_at=excluded.last_observed_at,
                      status=excluded.status,
                      recurrence_count=excluded.recurrence_count,
                      updated_at=excluded.updated_at
                    """,
                    (
                        state.user_id,
                        state.concept_id,
                        state.mastery_belief,
                        state.confidence,
                        state.evidence_count,
                        state.last_observed_at.isoformat() if state.last_observed_at else None,
                        state.status,
                        state.recurrence_count,
                        now,
                    ),
                )

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------
    def diagnose(
        self,
        user_id: str,
        target_concept_id: str,
        *,
        state_overrides: dict[str, ConceptState] | None = None,
        pending_event: EvidenceEvent | None = None,
    ) -> DiagnosisResult:
        target_row = self.db.fetchone(
            "SELECT subject_id FROM concepts WHERE concept_id=?", (target_concept_id,)
        )
        if not target_row:
            raise ValueError(f"Unknown concept_id: {target_concept_id}")

        personalization = self.memory.personalization_enabled(user_id)
        recent = (
            self.memory.recent_events(
                user_id=user_id, subject_id=target_row["subject_id"],
                concept_id=target_concept_id, limit=20,
            )
            if personalization else []
        )
        if pending_event is not None:
            if pending_event.user_id != user_id or pending_event.subject_id != target_row["subject_id"]:
                raise ValueError("Pending evidence does not match diagnosis identity")
            if pending_event.evidence_strength > 0:
                recent = [self._event_to_mapping(pending_event), *recent]
        state_overrides = state_overrides or {}
        stored_target = self.memory.get_concept_state(user_id, target_concept_id) if personalization else None
        if not recent and not state_overrides.get(target_concept_id) and not stored_target:
            return DiagnosisResult(
                target_concept_id=target_concept_id,
                hypotheses=[],
                entropy=0.0,
                confident=False,
                confidence_margin=0.0,
                recommended_action="insufficient_evidence",
            )

        candidates: dict[str, dict[str, Any]] = {
            target_concept_id: {"source": "direct", "path_strength": 1.0, "evidence": []}
        }
        for cid, strength, depth in self.prerequisites(target_concept_id):
            candidates[cid] = {
                "source": "prerequisite",
                "path_strength": strength,
                "evidence": [f"prerequisite_depth={depth}"],
            }
        for cid, strength in self.component_candidates(target_concept_id):
            candidates[cid] = {
                "source": "component",
                "path_strength": strength,
                "evidence": ["explicit_component_of_target"],
            }
        for cid in self.misconception_neighbors(target_concept_id):
            candidates.setdefault(
                cid,
                {
                    "source": "misconception",
                    "path_strength": 1.0,
                    "evidence": ["curriculum_confusion_link"],
                },
            )

        weights: dict[str, float] = {}
        explicit_evidence_seen = False
        for cid, info in candidates.items():
            state = state_overrides.get(cid) or (
                self.memory.get_concept_state(user_id, cid) if personalization else None
            )
            nonmastery = 1.0 - (state.mastery_belief if state else 0.5)
            prior_weight = max(1e-12, nonmastery * float(info["path_strength"]))
            log_weight = math.log(prior_weight)
            for event in recent:
                strength = float(event.get("evidence_strength", 1.0) or 0.0)
                strength = max(0.0, min(1.0, strength))
                if strength <= 0:
                    continue
                if event.get("primary_concept_id") == cid and event.get("correct") is False:
                    explicit_evidence_seen = True
                    info["evidence"].append(f"event:{event['event_id']} direct_incorrect_observation")
                # Historical evaluator factors are already reflected by persistent
                # concept state. Reapplying all of them here double-counts old
                # mistakes and can trap a recovered learner. Use likelihood factors
                # only for the current pending observation.
                likelihoods = event.get("concept_likelihoods", {})
                factor = (
                    likelihoods.get(cid)
                    if event.get("_pending") and isinstance(likelihoods, dict) else None
                )
                if factor is not None:
                    try:
                        factor = max(0.05, min(float(factor), 20.0))
                    except (TypeError, ValueError):
                        continue
                    # A factor of 1 is neutral and is not evidence by itself.
                    if not math.isclose(factor, 1.0, rel_tol=1e-9, abs_tol=1e-9):
                        log_weight += strength * math.log(factor)
                        explicit_evidence_seen = True
                        info["evidence"].append(
                            f"event:{event['event_id']} factor={round(factor,3)}"
                        )
            weights[cid] = log_weight

        # Stable softmax over log-weights prevents overflow/underflow after long
        # histories or strong evaluator likelihood factors.
        max_log = max(weights.values()) if weights else 0.0
        exp_weights = {cid: math.exp(v - max_log) for cid, v in weights.items()}
        total = sum(exp_weights.values())
        probs = (
            {cid: w / total for cid, w in exp_weights.items()}
            if total > 0 else {cid: 1.0 / len(weights) for cid in weights}
        )

        ranked = sorted(probs.items(), key=lambda x: x[1], reverse=True)
        hypotheses = [
            GapHypothesis(
                concept_id=cid,
                probability=prob,
                source=candidates[cid]["source"],
                evidence=candidates[cid]["evidence"],
            )
            for cid, prob in ranked
        ]
        entropy = self._entropy(probs.values())
        top = ranked[0][1] if ranked else 0.0
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = max(0.0, top - second)

        # A normalized distribution always has a winner, even when every concept
        # is well mastered. Require absolute evidence that the top hypothesis is
        # genuinely weak; otherwise the system would manufacture a "root gap" out
        # of relative probabilities after an isolated slip.
        top_id = ranked[0][0] if ranked else None
        top_state = (
            state_overrides.get(top_id)
            if top_id else None
        ) or (
            self.memory.get_concept_state(user_id, top_id)
            if top_id and personalization else None
        )
        pending_supports_top = False
        if pending_event is not None and top_id:
            pending_factor = pending_event.concept_likelihoods.get(top_id)
            pending_supports_top = bool(
                pending_event.evidence_strength > 0
                and pending_event.correct is False
                and (
                    pending_event.primary_concept_id == top_id
                    or (pending_factor is not None and pending_factor > 1.0)
                )
            )
        gap_supported = bool(
            pending_supports_top
            or (
                top_state is not None
                and (
                    top_state.mastery_belief < self.REVIEW_MASTERY
                    or top_state.status in {"active_blocker", "needs_review"}
                    or top_state.recurrence_count >= 2
                )
            )
        )
        confident = bool(
            explicit_evidence_seen and gap_supported
            and top >= self.DIAGNOSIS_TOP_THRESHOLD
            and margin >= self.DIAGNOSIS_MARGIN_THRESHOLD
        )
        target_state_now = state_overrides.get(target_concept_id) or stored_target
        no_current_gap = bool(
            not gap_supported and target_state_now is not None
            and target_state_now.mastery_belief >= self.RECOVERED_MASTERY
        )
        question = (
            None if confident or no_current_gap
            else self.select_diagnostic_question(target_row["subject_id"], hypotheses, user_id=user_id)
        )

        if confident:
            action = "teach"
        elif no_current_gap:
            action = "practice"
        elif question:
            action = "ask_diagnostic"
        elif hypotheses:
            action = "reassess"
        else:
            action = "insufficient_evidence"

        return DiagnosisResult(
            target_concept_id=target_concept_id,
            hypotheses=hypotheses,
            entropy=entropy,
            confident=confident,
            confidence_margin=margin,
            recommended_action=action,
            selected_question_id=question.question_id if question else None,
        )

    @staticmethod
    def _entropy(values: Any) -> float:
        h = 0.0
        for p in values:
            if p > 0:
                h -= p * math.log2(p)
        return h

    def get_question(self, question_id: str) -> QuestionRecord | None:
        row = self.db.fetchone("SELECT * FROM questions WHERE question_id=?", (question_id,))
        return self._question_from_row(row) if row else None

    def diagnostic_questions(self, subject_id: str, concept_ids: list[str]) -> list[QuestionRecord]:
        rows = self.db.fetchall(
            "SELECT * FROM questions WHERE subject_id=? AND is_diagnostic=1",
            (subject_id,),
        )
        wanted = set(concept_ids)
        result: list[QuestionRecord] = []
        for row in rows:
            q = self._question_from_row(row)
            covered = {q.primary_concept_id, *q.secondary_concept_ids}
            if not wanted or covered & wanted:
                result.append(q)
        return result

    def select_diagnostic_question(
        self, subject_id: str, hypotheses: list[GapHypothesis], user_id: str | None = None
    ) -> QuestionRecord | None:
        if not hypotheses:
            return None
        prior = {h.concept_id: h.probability for h in hypotheses}
        questions = self.diagnostic_questions(subject_id, list(prior))
        if user_id:
            recent_ids = self.memory.recent_question_ids(user_id, subject_id, limit=30)
            questions = [q for q in questions if q.question_id not in recent_ids]
        if not questions:
            return None

        best: tuple[float, int, QuestionRecord] | None = None
        for question in questions:
            ig = self._question_information_gain(prior, question.diagnostic_model)
            if ig is None:
                # No calibrated model: only use as a deterministic fallback if it
                # directly targets the highest-ranked hypothesis.
                fallback = int(question.primary_concept_id == hypotheses[0].concept_id)
                score = 0.0
            else:
                fallback = 1
                score = ig
            # On equal information, prefer shorter learner burden.
            candidate = (score, fallback, question)
            if best is None or candidate[0] > best[0] or (
                math.isclose(candidate[0], best[0], rel_tol=1e-12, abs_tol=1e-12)
                and (candidate[1] > best[1] or (candidate[1] == best[1] and len(question.stem) < len(best[2].stem)))
            ):
                best = candidate
        return best[2] if best else None

    def _question_information_gain(
        self,
        prior: dict[str, float],
        diagnostic_model: dict[str, dict[str, float]],
    ) -> float | None:
        """Expected information gain for a binary correct/incorrect outcome.

        diagnostic_model schema:
          {"gap_id": {"p_correct": 0.15}, ...}

        Missing hypothesis entries mean the item has not been calibrated for
        that hypothesis; in that case information gain is not claimed.
        """
        if not diagnostic_model or any(g not in diagnostic_model for g in prior):
            return None
        p_correct_given: dict[str, float] = {}
        for gap, model in diagnostic_model.items():
            try:
                p = float(model["p_correct"])
            except (KeyError, TypeError, ValueError):
                return None
            if not 0 <= p <= 1:
                return None
            p_correct_given[gap] = p

        current_h = self._entropy(prior.values())
        p_correct = sum(prior[g] * p_correct_given[g] for g in prior)
        expected_h = 0.0
        for outcome_correct, p_outcome in ((True, p_correct), (False, 1.0 - p_correct)):
            if p_outcome <= 1e-12:
                continue
            posterior: dict[str, float] = {}
            for g, p_gap in prior.items():
                likelihood = p_correct_given[g] if outcome_correct else 1.0 - p_correct_given[g]
                posterior[g] = p_gap * likelihood / p_outcome
            expected_h += p_outcome * self._entropy(posterior.values())
        return max(0.0, current_h - expected_h)

    def compute_event_update(self, event: EvidenceEvent) -> tuple[list[ConceptState], DiagnosisResult]:
        self.validate_event(event)
        states = self.update_states_for_event(event)
        overrides = {state.concept_id: state for state in states}
        diagnosis = self.diagnose(
            event.user_id, event.primary_concept_id,
            state_overrides=overrides, pending_event=event,
        )
        return states, diagnosis

    def process_event(self, event: EvidenceEvent) -> tuple[list[ConceptState], DiagnosisResult]:
        """Read/compute/commit under one in-process update lock."""
        with self.memory.write_lock:
            states, diagnosis = self.compute_event_update(event)
            if self.memory.personalization_enabled(event.user_id):
                self.memory.commit_learning_update(
                    event=event, states=states, hypotheses=diagnosis.hypotheses,
                    target_concept_id=event.primary_concept_id,
                )
            return states, diagnosis

    @staticmethod
    def _event_to_mapping(event: EvidenceEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "primary_concept_id": event.primary_concept_id,
            "concept_ids": event.concept_ids,
            "correct": event.correct,
            "score": event.score,
            "evidence_strength": event.evidence_strength,
            "mistake_type": event.mistake_type,
            "concept_likelihoods": event.concept_likelihoods,
            "timestamp": event.timestamp.isoformat(),
            "_pending": True,
        }

    @staticmethod
    def _question_from_row(row: Any) -> QuestionRecord:
        return QuestionRecord(
            question_id=row["question_id"],
            subject_id=row["subject_id"],
            primary_concept_id=row["primary_concept_id"],
            secondary_concept_ids=Database.loads(row["secondary_concept_ids_json"], []),
            stem=row["stem"],
            answer_type=row["answer_type"],
            answer_key=Database.loads(row["answer_key_json"], None),
            difficulty=row["difficulty"],
            is_diagnostic=bool(row["is_diagnostic"]),
            diagnostic_model=Database.loads(row["diagnostic_model_json"], {}),
            metadata=Database.loads(row["metadata_json"], {}),
        )
