from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import Settings
from .database import Database
from .learning import LearnerEngine
from .memory import MemoryService
from .schemas import (
    AdaptiveDecision,
    AdaptiveState,
    DiagnosisResult,
    EvidenceEvent,
    QuestionRecord,
    ResponseBehavior,
)


@dataclass(slots=True, frozen=True)
class AdaptivePolicy:
    """Configuration for the adaptive item-selection policy.

    The policy intentionally separates three jobs:
    - BKT in LearnerEngine estimates concept mastery over time.
    - This engine estimates a local ability/difficulty location for item routing.
    - LearnerEngine's information-gain logic handles diagnostic disambiguation.

    The defaults are product starting points and must be calibrated on real data.
    """

    target_success: float = 0.65
    micro_stage_size: int = 3
    max_difficulty_step: float = 0.08
    prior_theta: float = 0.0
    prior_variance: float = 1.50**2
    grid_min: float = -4.0
    grid_max: float = 4.0
    grid_points: int = 161

    @classmethod
    def from_settings(cls, settings: Settings) -> "AdaptivePolicy":
        return cls(
            target_success=settings.adaptive_target_success,
            micro_stage_size=settings.adaptive_micro_stage_size,
            max_difficulty_step=settings.adaptive_max_difficulty_step,
        )


class AdaptiveEngine:
    """Dynamic CAT-style difficulty routing plus response-behavior interpretation.

    This is *not* a clone of the SAT. The SAT uses multistage routing by modules.
    A tutor gets richer, lower-stakes evidence, so we keep a small micro-stage to
    prevent question-by-question thrashing while updating a Bayesian ability
    estimate after each informative response.

    Difficulty is continuous in [0, 1]. Easy/medium/hard are presentation bands.
    """

    def __init__(
        self,
        db: Database,
        memory: MemoryService,
        learner: LearnerEngine,
        settings: Settings,
    ):
        self.db = db
        self.memory = memory
        self.learner = learner
        self.policy = AdaptivePolicy.from_settings(settings)
        if not 0.50 <= self.policy.target_success <= 0.85:
            raise ValueError("adaptive_target_success should be in [0.50, 0.85]")
        if self.policy.micro_stage_size < 1:
            raise ValueError("adaptive_micro_stage_size must be >= 1")
        if not 0.01 <= self.policy.max_difficulty_step <= 0.50:
            raise ValueError("adaptive_max_difficulty_step should be in [0.01, 0.50]")

    # ------------------------------------------------------------------
    # Public state lifecycle
    # ------------------------------------------------------------------
    def _default_state(self, user_id: str, subject_id: str, concept_id: str) -> AdaptiveState:
        return AdaptiveState(
            user_id=user_id, subject_id=subject_id, concept_id=concept_id,
            theta=self.policy.prior_theta, variance=self.policy.prior_variance,
            target_difficulty=0.50, difficulty_band="medium", observations=0,
            micro_stage_position=0, behavior_profile="insufficient_evidence",
        )

    def get_state(self, user_id: str, subject_id: str, concept_id: str) -> AdaptiveState:
        concept = self.db.fetchone(
            "SELECT subject_id FROM concepts WHERE concept_id=?", (concept_id,)
        )
        if not concept or concept["subject_id"] != subject_id:
            raise ValueError("Unknown concept or subject/concept mismatch")
        if not self.memory.personalization_enabled(user_id):
            return self._default_state(user_id, subject_id, concept_id)
        row = self.db.fetchone(
            """SELECT * FROM adaptive_state
               WHERE user_id=? AND subject_id=? AND concept_id=?""",
            (user_id, subject_id, concept_id),
        )
        if not row:
            return self._default_state(user_id, subject_id, concept_id)
        return AdaptiveState(
            user_id=row["user_id"], subject_id=row["subject_id"], concept_id=row["concept_id"],
            theta=float(row["theta"]), variance=float(row["variance"]),
            target_difficulty=float(row["target_difficulty"]),
            difficulty_band=row["difficulty_band"], observations=int(row["observations"]),
            micro_stage_position=int(row["micro_stage_position"]),
            behavior_profile=row["behavior_profile"],
        )

    def update_from_event(self, event: EvidenceEvent, *, persist: bool = True) -> AdaptiveState:
        """Update item-difficulty ability only when the event has usable outcome evidence.

        Mastery and ability are intentionally different. A correct answer with two
        hints is still a success, but it is weaker *independent* evidence. Hints,
        repeated attempts and evaluator confidence change evidence weight rather
        than rewriting correctness.
        """
        previous = self.get_state(event.user_id, event.subject_id, event.primary_concept_id)
        score = self._event_score(event)
        if score is None:
            return previous

        behavior = self._behavior_from_event(event)
        weight = self._evidence_weight(event, behavior)
        if weight <= 1e-6:
            return previous

        difficulty = self._event_difficulty(event)
        discrimination = self._event_discrimination(event)
        theta, variance = self._eap_update(
            mean=previous.theta,
            variance=previous.variance,
            item_difficulty=difficulty,
            discrimination=discrimination,
            score=score,
            weight=weight,
        )

        observations = previous.observations + 1
        stage_position = observations % self.policy.micro_stage_size
        unconstrained_target = self._target_difficulty_from_theta(theta)

        # During a micro-stage, allow only fine movement. At a stage boundary the
        # same capped update may cross into a neighboring band. This prevents the
        # simplistic easy→hard→easy oscillation that happens with one-item rules.
        target = self._bounded_move(
            previous.target_difficulty,
            unconstrained_target,
            self.policy.max_difficulty_step,
        )
        if observations < self.policy.micro_stage_size:
            # Initial calibration stays closer to the middle until a few items exist.
            calibration_weight = observations / self.policy.micro_stage_size
            target = 0.50 * (1.0 - calibration_weight) + target * calibration_weight

        # SAT-style stability without copying SAT literally: the first micro-stage
        # is a medium calibration block. After that, the continuous estimate is
        # updated every response, but easy/medium/hard band changes happen only
        # at micro-stage boundaries. Difficulty can still move smoothly inside
        # the current band.
        stage_boundary = observations % self.policy.micro_stage_size == 0
        if observations < self.policy.micro_stage_size:
            target = self._clamp_to_band(target, "medium")
            band = "medium"
        elif not stage_boundary:
            band = previous.difficulty_band
            target = self._clamp_to_band(target, band)
        else:
            desired_band = self._band(target)
            band = self._one_band_step(previous.difficulty_band, desired_band)
            target = self._clamp_to_band(target, band)

        new_state = AdaptiveState(
            user_id=event.user_id,
            subject_id=event.subject_id,
            concept_id=event.primary_concept_id,
            theta=theta,
            variance=variance,
            target_difficulty=target,
            difficulty_band=band,
            observations=observations,
            micro_stage_position=stage_position,
            behavior_profile=self._behavior_profile(score, behavior),
        )
        if persist and self.memory.personalization_enabled(event.user_id):
            self._persist_state(new_state)
        return new_state

    def rebuild_user_state(self, user_id: str, concept_ids: set[str] | None = None) -> None:
        if concept_ids:
            values = list(concept_ids)
            with self.db.transaction() as conn:
                for start in range(0, len(values), 400):
                    batch = values[start:start + 400]
                    placeholders = ",".join("?" for _ in batch)
                    conn.execute(
                        f"DELETE FROM adaptive_state WHERE user_id=? AND concept_id IN ({placeholders})",
                        (user_id, *batch),
                    )
        else:
            self.db.execute("DELETE FROM adaptive_state WHERE user_id=?", (user_id,))

        rows = self.db.fetchall(
            "SELECT * FROM learning_events WHERE user_id=? ORDER BY timestamp,event_id",
            (user_id,),
        )
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
            if concept_ids is None or event.primary_concept_id in concept_ids:
                self.update_from_event(event)

    # ------------------------------------------------------------------
    # Next action / question selection
    # ------------------------------------------------------------------
    def recommend_next(
        self,
        *,
        user_id: str,
        subject_id: str,
        concept_id: str,
        diagnosis: DiagnosisResult | None = None,
    ) -> AdaptiveDecision:
        concept = self.db.fetchone(
            "SELECT subject_id,name FROM concepts WHERE concept_id=?",
            (concept_id,),
        )
        if not concept or concept["subject_id"] != subject_id:
            raise ValueError("Unknown concept or subject/concept mismatch")

        diagnosis = diagnosis or self.learner.diagnose(user_id, concept_id)
        state = self.get_state(user_id, subject_id, concept_id)

        # Diagnosis has priority over challenge level: if we do not know what is
        # wrong, an informative diagnostic is more valuable than a harder item.
        concept_state = (
            self.memory.get_concept_state(user_id, concept_id)
            if self.memory.personalization_enabled(user_id) else None
        )
        needs_gap_diagnostic = bool(
            diagnosis.recommended_action == "ask_diagnostic"
            and diagnosis.selected_question_id
            and (
                concept_state is None
                or concept_state.mastery_belief < 0.70
                or concept_state.status in {"active_blocker", "needs_review"}
                or self._recent_failure(user_id, subject_id, concept_id)
            )
        )
        if needs_gap_diagnostic:
            question = self.learner.get_question(diagnosis.selected_question_id)
            return AdaptiveDecision(
                mode="diagnostic",
                target_concept_id=concept_id,
                selected_question_id=question.question_id if question else diagnosis.selected_question_id,
                target_difficulty=state.target_difficulty,
                difficulty_band=self._band(state.target_difficulty),
                theta=state.theta,
                uncertainty=math.sqrt(max(state.variance, 0.0)),
                expected_success=self._expected_success(state.theta, question) if question else None,
                reason="diagnostic_uncertainty",
                behavior_profile=state.behavior_profile,
                micro_stage_position=state.micro_stage_position,
                micro_stage_size=self.policy.micro_stage_size,
            )

        target_concept = concept_id
        if diagnosis.confident and diagnosis.hypotheses:
            top = diagnosis.hypotheses[0]
            if top.source in {"prerequisite", "component"}:
                target_concept = top.concept_id

        target_state = self.get_state(user_id, subject_id, target_concept)
        questions = self._practice_candidates(subject_id, target_concept, user_id)
        question = self._select_practice_question(questions, target_state)

        if question is None:
            reason = "no_validated_item_available"
            selected_id = None
            expected = None
            target_difficulty = target_state.target_difficulty
        else:
            reason = "ability_matched_practice"
            selected_id = question.question_id
            expected = self._expected_success(target_state.theta, question)
            target_difficulty = target_state.target_difficulty

        return AdaptiveDecision(
            mode="practice",
            target_concept_id=target_concept,
            selected_question_id=selected_id,
            target_difficulty=target_difficulty,
            difficulty_band=self._band(target_difficulty),
            theta=target_state.theta,
            uncertainty=math.sqrt(max(target_state.variance, 0.0)),
            expected_success=expected,
            reason=reason,
            behavior_profile=target_state.behavior_profile,
            micro_stage_position=target_state.micro_stage_position,
            micro_stage_size=self.policy.micro_stage_size,
        )

    # ------------------------------------------------------------------
    # Bayesian ability update
    # ------------------------------------------------------------------
    def _eap_update(
        self,
        *,
        mean: float,
        variance: float,
        item_difficulty: float,
        discrimination: float,
        score: float,
        weight: float,
    ) -> tuple[float, float]:
        variance = max(variance, 0.05**2)
        sd = math.sqrt(variance)
        grid = self._grid()
        b = self._difficulty_to_b(item_difficulty)
        a = max(0.25, min(3.0, discrimination))
        y = max(0.0, min(1.0, score))
        w = max(1e-6, min(1.0, weight))

        log_weights: list[float] = []
        for theta in grid:
            z = (theta - mean) / sd
            log_prior = -0.5 * z * z - math.log(sd)
            p = self._logistic(a * (theta - b))
            p = max(1e-9, min(1.0 - 1e-9, p))
            # Fractional-Bernoulli likelihood allows partial-credit evidence while
            # keeping a conventional binary IRT response model underneath.
            log_like = w * (y * math.log(p) + (1.0 - y) * math.log(1.0 - p))
            log_weights.append(log_prior + log_like)

        peak = max(log_weights)
        weights = [math.exp(v - peak) for v in log_weights]
        total = sum(weights)
        if total <= 0 or not math.isfinite(total):
            return mean, variance
        posterior = [v / total for v in weights]
        new_mean = sum(t * p for t, p in zip(grid, posterior, strict=True))
        new_var = sum(((t - new_mean) ** 2) * p for t, p in zip(grid, posterior, strict=True))
        return float(new_mean), float(max(new_var, 0.05**2))

    def _grid(self) -> list[float]:
        n = self.policy.grid_points
        step = (self.policy.grid_max - self.policy.grid_min) / (n - 1)
        return [self.policy.grid_min + i * step for i in range(n)]

    @staticmethod
    def _logistic(x: float) -> float:
        if x >= 0:
            z = math.exp(-x)
            return 1.0 / (1.0 + z)
        z = math.exp(x)
        return z / (1.0 + z)

    @staticmethod
    def _difficulty_to_b(difficulty: float) -> float:
        # Map product difficulty [0,1] to a practical IRT location [-3,3].
        return 6.0 * (max(0.0, min(1.0, difficulty)) - 0.5)

    @staticmethod
    def _b_to_difficulty(b: float) -> float:
        return max(0.0, min(1.0, b / 6.0 + 0.5))

    def _target_difficulty_from_theta(self, theta: float) -> float:
        p = self.policy.target_success
        # For 1PL, P(correct)=sigmoid(theta-b). Solve for b at desired p.
        logit = math.log(p / (1.0 - p))
        return self._b_to_difficulty(theta - logit)

    @staticmethod
    def _bounded_move(previous: float, desired: float, max_step: float) -> float:
        low = previous - max_step
        high = previous + max_step
        return max(0.0, min(1.0, max(low, min(high, desired))))

    # ------------------------------------------------------------------
    # Question selection
    # ------------------------------------------------------------------
    def _practice_candidates(
        self, subject_id: str, concept_id: str, user_id: str
    ) -> list[QuestionRecord]:
        rows = self.db.fetchall(
            "SELECT * FROM questions WHERE subject_id=? AND is_diagnostic=0",
            (subject_id,),
        )
        recent_ids = self.memory.recent_question_ids(user_id, subject_id, limit=24)
        direct_all: list[QuestionRecord] = []
        secondary_all: list[QuestionRecord] = []
        for row in rows:
            q = self.learner._question_from_row(row)
            if q.primary_concept_id == concept_id:
                direct_all.append(q)
            elif concept_id in q.secondary_concept_ids:
                secondary_all.append(q)
        pool = direct_all or secondary_all
        fresh = [q for q in pool if q.question_id not in recent_ids]
        return fresh or pool

    def _select_practice_question(
        self, questions: list[QuestionRecord], state: AdaptiveState
    ) -> QuestionRecord | None:
        if not questions:
            return None

        best: tuple[float, float, float, str, QuestionRecord] | None = None
        for q in questions:
            p = self._expected_success(state.theta, q)
            if p is None:
                continue
            discrimination = self._question_discrimination(q)
            info = discrimination**2 * p * (1.0 - p)
            success_gap = abs(p - self.policy.target_success)
            difficulty_gap = abs(q.difficulty - state.target_difficulty)
            # Lexicographic, not a fabricated weighted sum: first match the
            # educational success target, then prefer informative and nearby items.
            key = (success_gap, -info, difficulty_gap, q.question_id, q)
            if best is None or key[:4] < best[:4]:
                best = key
        return best[4] if best else None

    def _expected_success(self, theta: float, question: QuestionRecord | None) -> float | None:
        if question is None:
            return None
        b = self._difficulty_to_b(question.difficulty)
        a = self._question_discrimination(question)
        return self._logistic(a * (theta - b))

    @staticmethod
    def _question_discrimination(question: QuestionRecord) -> float:
        raw = question.metadata.get("irt_discrimination", 1.0)
        try:
            return max(0.25, min(3.0, float(raw)))
        except (TypeError, ValueError):
            return 1.0

    def _recent_question_ids(self, user_id: str, subject_id: str, limit: int) -> set[str]:
        return self.memory.recent_question_ids(user_id, subject_id, limit=limit)

    # ------------------------------------------------------------------
    # Answer-behavior interpretation
    # ------------------------------------------------------------------
    @staticmethod
    def _event_score(event: EvidenceEvent) -> float | None:
        if event.score is not None:
            return float(event.score)
        if event.correct is not None:
            return 1.0 if event.correct else 0.0
        return None

    @staticmethod
    def _event_difficulty(event: EvidenceEvent) -> float:
        try:
            return max(0.0, min(1.0, float(event.metadata.get("question_difficulty", 0.5))))
        except (TypeError, ValueError):
            return 0.5

    @staticmethod
    def _event_discrimination(event: EvidenceEvent) -> float:
        try:
            return max(0.25, min(3.0, float(event.metadata.get("irt_discrimination", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _behavior_from_event(event: EvidenceEvent) -> ResponseBehavior:
        raw = event.metadata.get("behavior") or {}
        if not isinstance(raw, dict):
            raw = {}
        try:
            return ResponseBehavior.model_validate(raw)
        except ValueError:
            # Malformed optional behavior metadata is ignored rather than making
            # the core learning event impossible to record.
            return ResponseBehavior()

    @staticmethod
    def _evidence_weight(event: EvidenceEvent, behavior: ResponseBehavior) -> float:
        weight = max(0.0, min(1.0, event.evidence_strength))
        if weight <= 0.0:
            return 0.0
        if behavior.hint_count > 0:
            weight /= 1.0 + 0.35 * behavior.hint_count
        if behavior.attempt_count > 1:
            weight /= 1.0 + 0.15 * (behavior.attempt_count - 1)
        if behavior.evaluator_confidence is not None:
            weight *= max(0.1, min(1.0, behavior.evaluator_confidence))
        # Response time does not reduce mastery/ability evidence. Speed is noisy
        # across disability, language, device and context; it is used only to
        # shape pedagogy in _behavior_profile.
        return max(0.0, min(1.0, weight))

    @staticmethod
    def _behavior_profile(score: float, behavior: ResponseBehavior) -> str:
        correctish = score >= 0.75
        confidence = behavior.self_confidence
        if not correctish and confidence is not None and confidence >= 0.75:
            return "confident_error_misconception_risk"
        if correctish and behavior.hint_count >= 2:
            return "supported_success"
        if correctish and behavior.attempt_count > 1:
            return "recovered_after_retry"
        if correctish and confidence is not None and confidence <= 0.35:
            return "fragile_confidence"
        if not correctish and behavior.hint_count > 0:
            return "needs_scaffolding"
        if (
            behavior.response_seconds is not None
            and behavior.expected_seconds is not None
            and behavior.expected_seconds > 0
            and behavior.response_seconds > behavior.expected_seconds * 1.75
        ):
            return "slow_or_deliberate_response"
        return "independent_success" if correctish else "independent_error"

    def _recent_failure(self, user_id: str, subject_id: str, concept_id: str) -> bool:
        if not self.memory.personalization_enabled(user_id):
            return False
        rows = self.db.fetchall(
            """
            SELECT primary_concept_id,concept_ids_json,correct,score
            FROM learning_events
            WHERE user_id=? AND subject_id=?
            ORDER BY timestamp DESC LIMIT 12
            """,
            (user_id, subject_id),
        )
        for row in rows:
            concepts = self.db.loads(row["concept_ids_json"], [])
            if row["primary_concept_id"] != concept_id and concept_id not in concepts:
                continue
            if row["correct"] is not None:
                return not bool(row["correct"])
            if row["score"] is not None:
                return float(row["score"]) < 0.60
            return False
        return False

    @staticmethod
    def _clamp_to_band(difficulty: float, band: str) -> float:
        if band == "easy":
            return max(0.0, min(0.399, difficulty))
        if band == "hard":
            return max(0.70, min(1.0, difficulty))
        return max(0.40, min(0.699, difficulty))

    @staticmethod
    def _one_band_step(current: str, desired: str) -> str:
        order = ["easy", "medium", "hard"]
        try:
            i = order.index(current)
            j = order.index(desired)
        except ValueError:
            return desired
        if j > i:
            return order[i + 1]
        if j < i:
            return order[i - 1]
        return current

    # ------------------------------------------------------------------
    # Persistence / presentation helpers
    # ------------------------------------------------------------------
    def _persist_state(self, state: AdaptiveState) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """
            INSERT INTO adaptive_state(
              user_id,subject_id,concept_id,theta,variance,target_difficulty,
              difficulty_band,observations,micro_stage_position,behavior_profile,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,subject_id,concept_id) DO UPDATE SET
              theta=excluded.theta,
              variance=excluded.variance,
              target_difficulty=excluded.target_difficulty,
              difficulty_band=excluded.difficulty_band,
              observations=excluded.observations,
              micro_stage_position=excluded.micro_stage_position,
              behavior_profile=excluded.behavior_profile,
              updated_at=excluded.updated_at
            """,
            (
                state.user_id,
                state.subject_id,
                state.concept_id,
                state.theta,
                state.variance,
                state.target_difficulty,
                state.difficulty_band,
                state.observations,
                state.micro_stage_position,
                state.behavior_profile,
                now,
            ),
        )

    @staticmethod
    def _band(difficulty: float) -> str:
        if difficulty < 0.40:
            return "easy"
        if difficulty < 0.70:
            return "medium"
        return "hard"
