from __future__ import annotations

from adaptive_backend.adaptive import AdaptiveEngine
from adaptive_backend.schemas import EvidenceEvent


def _event(session_id: str, user_id: str, correct: bool, *, hints: int = 0, confidence=None):
    return EvidenceEvent(
        session_id=session_id,
        user_id=user_id,
        subject_id="DSA",
        concept_ids=["binary_search"],
        primary_concept_id="binary_search",
        activity_type="answer_short",
        correct=correct,
        score=1.0 if correct else 0.0,
        evidence_strength=1.0,
        metadata={
            "question_id": "dsa_problem_binary_search",
            "question_difficulty": 0.45,
            "irt_discrimination": 1.15,
            "behavior": {
                "hint_count": hints,
                "attempt_count": 1,
                "self_confidence": confidence,
            },
        },
    )


def test_initial_adaptive_question_starts_near_medium(core, settings):
    db, retrieval, memory, learner = core
    adaptive = AdaptiveEngine(db, memory, learner, settings)
    decision = adaptive.recommend_next(
        user_id="new-user", subject_id="DSA", concept_id="binary_search"
    )
    assert decision.mode == "practice"
    assert decision.selected_question_id == "dsa_problem_binary_search"
    assert decision.difficulty_band == "medium"


def test_repeated_success_raises_ability_and_difficulty(core, settings):
    db, retrieval, memory, learner = core
    adaptive = AdaptiveEngine(db, memory, learner, settings)
    session = memory.start_session(user_id="up", subject_id="DSA", goal=None, source="text")

    state = adaptive.get_state("up", "DSA", "binary_search")
    initial_theta = state.theta
    for _ in range(9):
        event = _event(session.session_id, "up", True)
        learner.process_event(event)
        state = adaptive.update_from_event(event)

    assert state.theta > initial_theta
    assert state.target_difficulty > 0.50
    assert state.difficulty_band == "hard"
    decision = adaptive.recommend_next(user_id="up", subject_id="DSA", concept_id="binary_search")
    assert decision.mode == "practice"
    assert decision.selected_question_id == "dsa_binary_hard"


def test_repeated_failure_routes_down_without_one_item_thrashing(core, settings):
    db, retrieval, memory, learner = core
    adaptive = AdaptiveEngine(db, memory, learner, settings)
    session = memory.start_session(user_id="down", subject_id="DSA", goal=None, source="text")

    first = None
    third = None
    for i in range(3):
        event = _event(session.session_id, "down", False)
        learner.process_event(event)
        state = adaptive.update_from_event(event)
        if i == 0:
            first = state
        if i == 2:
            third = state

    assert first is not None and first.difficulty_band == "medium"
    assert third is not None and third.difficulty_band == "easy"
    assert third.target_difficulty < 0.40


def test_hints_reduce_ability_evidence_not_correctness(core, settings):
    db, retrieval, memory, learner = core
    adaptive = AdaptiveEngine(db, memory, learner, settings)
    s1 = memory.start_session(user_id="independent", subject_id="DSA", goal=None, source="text")
    s2 = memory.start_session(user_id="supported", subject_id="DSA", goal=None, source="text")

    e1 = _event(s1.session_id, "independent", True, hints=0)
    e2 = _event(s2.session_id, "supported", True, hints=2)
    learner.process_event(e1)
    learner.process_event(e2)
    a = adaptive.update_from_event(e1)
    b = adaptive.update_from_event(e2)

    assert a.theta > b.theta
    assert b.behavior_profile == "supported_success"


def test_confident_wrong_answer_flags_misconception_risk(core, settings):
    db, retrieval, memory, learner = core
    adaptive = AdaptiveEngine(db, memory, learner, settings)
    session = memory.start_session(user_id="confident-wrong", subject_id="DSA", goal=None, source="text")
    event = _event(session.session_id, "confident-wrong", False, confidence=0.95)
    learner.process_event(event)
    state = adaptive.update_from_event(event)
    assert state.behavior_profile == "confident_error_misconception_risk"


def test_adaptive_state_is_subject_scoped(core, settings):
    db, retrieval, memory, learner = core
    adaptive = AdaptiveEngine(db, memory, learner, settings)
    dsa = adaptive.get_state("same-user", "DSA", "binary_search")
    bio = adaptive.get_state("same-user", "BIO", "bio_golgi")
    assert dsa.subject_id == "DSA"
    assert bio.subject_id == "BIO"
    assert dsa.concept_id != bio.concept_id
