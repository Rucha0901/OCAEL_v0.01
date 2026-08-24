from __future__ import annotations

from adaptive_backend.schemas import EvidenceEvent


def test_bkt_updates_and_persists_state(core):
    db, retrieval, memory, learner = core
    session = memory.start_session(user_id="u1", subject_id="BIO", goal=None, source="text")
    event = EvidenceEvent(
        session_id=session.session_id,
        user_id="u1",
        subject_id="BIO",
        concept_ids=["bio_golgi", "bio_rer"],
        primary_concept_id="bio_golgi",
        activity_type="answer_mcq",
        correct=False,
        evidence_strength=1.0,
        mistake_type="rer_golgi_function_confusion",
        concept_likelihoods={"bio_golgi": 7.0, "bio_rer": 3.0},
    )
    states, diagnosis = learner.process_event(event)
    assert len(states) == 2
    golgi = memory.get_concept_state("u1", "bio_golgi")
    assert golgi is not None
    assert golgi.mastery_belief < 0.2
    assert diagnosis.hypotheses
    assert diagnosis.hypotheses[0].concept_id in {"bio_golgi", "bio_rer", "bio_cell"}


def test_information_gain_selects_calibrated_question(core):
    db, retrieval, memory, learner = core
    session = memory.start_session(user_id="u2", subject_id="BIO", goal=None, source="text")
    event = EvidenceEvent(
        session_id=session.session_id,
        user_id="u2",
        subject_id="BIO",
        concept_ids=["bio_golgi", "bio_rer"],
        primary_concept_id="bio_golgi",
        activity_type="answer_mcq",
        correct=False,
        mistake_type="rer_golgi_function_confusion",
        concept_likelihoods={"bio_golgi": 1.5, "bio_rer": 1.5},
    )
    learner.process_event(event)
    result = learner.diagnose("u2", "bio_golgi")
    assert result.recommended_action in {"ask_diagnostic", "teach", "reassess"}
    if result.recommended_action == "ask_diagnostic":
        assert result.selected_question_id is not None


def test_code_component_can_be_diagnosed_as_blocker(core):
    db, retrieval, memory, learner = core
    session = memory.start_session(user_id="u-code", subject_id="DSA", goal=None, source="code")
    event = EvidenceEvent(
        session_id=session.session_id,
        user_id="u-code",
        subject_id="DSA",
        concept_ids=["binary_search", "binary_search_boundary", "binary_search_loop_invariant"],
        primary_concept_id="binary_search",
        activity_type="code_submission",
        correct=False,
        mistake_type="low_assigned_mid_without_visible_increment",
        concept_likelihoods={"binary_search_boundary": 7.0, "binary_search_loop_invariant": 3.0},
        metadata={"concept_strengths": {"binary_search_boundary": 0.7, "binary_search_loop_invariant": 0.5}},
    )
    _, result = learner.process_event(event)
    assert result.hypotheses[0].concept_id == "binary_search_boundary"
    assert any(h.source == "component" for h in result.hypotheses)


def test_paused_personalization_does_not_persist(core):
    db, retrieval, memory, learner = core
    session = memory.start_session(user_id="paused", subject_id="BIO", goal=None, source="text")
    memory.set_personalization("paused", False)
    event = EvidenceEvent(
        session_id=session.session_id,
        user_id="paused",
        subject_id="BIO",
        concept_ids=["bio_golgi"],
        primary_concept_id="bio_golgi",
        activity_type="answer_mcq",
        correct=False,
        concept_likelihoods={"bio_golgi": 5.0},
    )
    states, result = learner.process_event(event)
    assert states
    assert result.hypotheses
    assert memory.recent_events(user_id="paused", subject_id="BIO") == []
    assert memory.get_concept_state("paused", "bio_golgi") is None
