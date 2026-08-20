from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from adaptive_backend.adaptive import AdaptiveEngine
from adaptive_backend.app import create_app
from adaptive_backend.schemas import EvidenceEvent, KnowledgeChunkIn, SubjectPackIn, TheoryAnswerRequest
from adaptive_backend.subjects import TheoryService
from adaptive_backend.models import ModelGateway


def test_zero_strength_does_not_move_adaptive_state(core, settings):
    db, retrieval, memory, learner = core
    adaptive = AdaptiveEngine(db, memory, learner, settings)
    session = memory.start_session(user_id="zero", subject_id="DSA", goal=None, source="text")
    before = adaptive.get_state("zero", "DSA", "binary_search")
    event = EvidenceEvent(
        session_id=session.session_id, user_id="zero", subject_id="DSA",
        concept_ids=["binary_search"], primary_concept_id="binary_search",
        activity_type="syntax_only", correct=False, evidence_strength=0.0,
        metadata={"question_difficulty": 0.8},
    )
    after = adaptive.update_from_event(event)
    assert after.theta == before.theta
    assert after.observations == before.observations == 0


def test_paused_personalization_does_not_read_old_state(core, settings):
    db, retrieval, memory, learner = core
    adaptive = AdaptiveEngine(db, memory, learner, settings)
    session = memory.start_session(user_id="paused", subject_id="BIO", goal=None, source="text")
    learner.process_event(EvidenceEvent(
        session_id=session.session_id, user_id="paused", subject_id="BIO",
        concept_ids=["bio_golgi"], primary_concept_id="bio_golgi",
        activity_type="answer_mcq", correct=False,
        concept_likelihoods={"bio_golgi": 4.0},
    ))
    # Seed adaptive history too.
    adaptive.update_from_event(EvidenceEvent(
        session_id=session.session_id, user_id="paused", subject_id="BIO",
        concept_ids=["bio_golgi"], primary_concept_id="bio_golgi",
        activity_type="answer_mcq", correct=False,
    ))
    memory.set_personalization("paused", False)
    capsule = memory.build_context_capsule(
        user_id="paused", subject_id="BIO", target_concept_id="bio_golgi"
    )
    assert capsule["states"] == {}
    assert capsule["relevant_history"] == []
    assert capsule["gap_hypotheses"] == []
    state = adaptive.get_state("paused", "BIO", "bio_golgi")
    assert state.observations == 0 and state.theta == adaptive.policy.prior_theta
    diagnosis = learner.diagnose("paused", "bio_golgi")
    assert diagnosis.recommended_action == "insufficient_evidence"


def test_partial_delete_does_not_sweep_unrelated_empty_session(core):
    db, retrieval, memory, learner = core
    bio = memory.start_session(user_id="deleter", subject_id="BIO", goal=None, source="text")
    dsa = memory.start_session(user_id="deleter", subject_id="DSA", goal=None, source="text")
    learner.process_event(EvidenceEvent(
        session_id=bio.session_id, user_id="deleter", subject_id="BIO",
        concept_ids=["bio_golgi"], primary_concept_id="bio_golgi",
        activity_type="answer_mcq", correct=False,
    ))
    memory.delete_memory(user_id="deleter", subject_id="BIO")
    assert memory.get_session(dsa.session_id) is not None


def test_delete_all_preserves_privacy_choice_and_removes_adaptive(core, settings):
    db, retrieval, memory, learner = core
    adaptive = AdaptiveEngine(db, memory, learner, settings)
    s = memory.start_session(user_id="wipe", subject_id="DSA", goal=None, source="text")
    e = EvidenceEvent(
        session_id=s.session_id, user_id="wipe", subject_id="DSA",
        concept_ids=["binary_search"], primary_concept_id="binary_search",
        activity_type="answer", correct=True,
    )
    learner.process_event(e)
    adaptive.update_from_event(e)
    memory.set_personalization("wipe", False)
    memory.delete_memory(user_id="wipe", delete_all=True)
    assert memory.personalization_enabled("wipe") is False
    assert db.fetchone("SELECT 1 FROM adaptive_state WHERE user_id='wipe'") is None


def test_subject_pack_cannot_hijack_existing_concept(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/subjects/packs", json={
            "subject_id": "MATH", "name": "Math",
            "concepts": [{"concept_id": "binary_search", "name": "Hijack"}],
            "edges": [], "questions": [],
        })
        assert response.status_code == 400
        owner = app.state.services.db.fetchone(
            "SELECT subject_id FROM concepts WHERE concept_id='binary_search'"
        )
        assert owner["subject_id"] == "DSA"


def test_retrieval_filters_concept_before_top_k(core):
    db, retrieval, memory, learner = core
    for i in range(80):
        retrieval.upsert_chunk(KnowledgeChunkIn(
            chunk_id=f"noise-{i}", source_id="noise", subject_id="BIO",
            concept_ids=["bio_rer"], content_type="definition",
            content="golgi packaging proteins " * 4,
        ))
    retrieval.upsert_chunk(KnowledgeChunkIn(
        chunk_id="target-perox", source_id="target", subject_id="BIO",
        concept_ids=["bio_peroxisome"], content_type="definition",
        content="peroxisome catalase breaks down hydrogen peroxide",
    ))
    hits = retrieval.search(
        query="catalase peroxide", subject_id="BIO",
        concept_ids=["bio_peroxisome"], limit=2,
    )
    assert hits and hits[0].chunk_id == "target-perox"


def test_cross_subject_event_rejected(core):
    db, retrieval, memory, learner = core
    s = memory.start_session(user_id="cross", subject_id="BIO", goal=None, source="text")
    with pytest.raises(ValueError):
        learner.process_event(EvidenceEvent(
            session_id=s.session_id, user_id="cross", subject_id="BIO",
            concept_ids=["bio_golgi", "binary_search"], primary_concept_id="bio_golgi",
            activity_type="bad", correct=False,
        ))


def test_short_answer_list_key_is_not_nested(core, settings):
    db, retrieval, memory, learner = core
    models = ModelGateway(settings)
    theory = TheoryService(db, retrieval, models)
    # Existing medium practice item stores a list answer key.
    s = memory.start_session(user_id="short-list", subject_id="BIO", goal=None, source="text")
    evaluation, _ = theory.evaluate(TheoryAnswerRequest(
        session_id=s.session_id, user_id="short-list",
        question_id="bio_practice_golgi_medium", answer="golgi",
    ))
    assert evaluation.correct is True


def test_code_syntax_error_does_not_lower_algorithm_mastery(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        s = client.post("/sessions", json={"user_id":"syntax","subject_id":"DSA","source":"text"}).json()
        r = client.post("/code/analyze", json={
            "session_id": s["session_id"], "user_id":"syntax", "subject_id":"DSA",
            "concept_id":"binary_search", "problem_id":"dsa_problem_binary_search",
            "code":"def binary_search(:\n    pass"
        })
        assert r.status_code == 200
        state = app.state.services.memory.get_concept_state("syntax", "binary_search")
        assert state is None or state.evidence_count == 0
        adaptive = app.state.services.adaptive.get_state("syntax", "DSA", "binary_search")
        assert adaptive.observations == 0


def test_question_presented_counts_as_recent(core, settings):
    db, retrieval, memory, learner = core
    s = memory.start_session(user_id="repeat", subject_id="DSA", goal=None, source="text")
    memory.record_intervention(
        session_id=s.session_id, user_id="repeat", subject_id="DSA",
        concept_id="binary_search", action_type="adaptive_practice",
        content="question", grounded=True, source_ids=[], supervisor_used=False,
        metadata={"question_id": "dsa_problem_binary_search"},
    )
    assert "dsa_problem_binary_search" in memory.recent_question_ids("repeat", "DSA")


def test_raw_memory_endpoint_is_not_public(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/memory/events").status_code == 404


def test_fts_fallback_still_retrieves(core):
    db, retrieval, memory, learner = core
    db.fts_available = False
    hits = retrieval.search(
        query="packages proteins", subject_id="BIO", concept_ids=["bio_golgi"], limit=2
    )
    assert hits


def test_local_code_harness_ignores_student_stdout(settings):
    enabled = replace(settings, allow_local_code_execution=True)
    app = create_app(enabled)
    with TestClient(app) as client:
        s = client.post("/sessions", json={"user_id":"stdout","subject_id":"DSA","source":"code"}).json()
        code = '''def binary_search(arr, target):\n    print("student noise")\n    lo, hi = 0, len(arr)-1\n    while lo <= hi:\n        mid=(lo+hi)//2\n        if arr[mid] == target: return mid\n        if arr[mid] < target: lo=mid+1\n        else: hi=mid-1\n    return -1\n'''
        r = client.post("/code/analyze?run_tests=true&record=false", json={
            "session_id":s["session_id"],"user_id":"stdout","subject_id":"DSA",
            "concept_id":"binary_search","problem_id":"dsa_problem_binary_search","code":code,
        })
        assert r.status_code == 200
        assert r.json()["test_summary"]["status"] == "ok"
        assert r.json()["test_summary"]["failed"] == 0


def test_compaction_advances_past_first_batch(core):
    db, retrieval, memory, learner = core
    # Threshold fixture is 10; 15 completed sessions should produce at least two
    # non-overlapping compactions rather than repeatedly seeing the first batch.
    for i in range(15):
        s = memory.start_session(user_id="compact", subject_id="BIO", goal=f"s{i}", source="text")
        learner.process_event(EvidenceEvent(
            session_id=s.session_id, user_id="compact", subject_id="BIO",
            concept_ids=["bio_golgi"], primary_concept_id="bio_golgi",
            activity_type="answer", correct=bool(i % 2),
        ))
        memory.complete_session(s.session_id)
    rows = db.fetchall(
        "SELECT start_at,end_at,source_session_ids_json FROM compacted_history WHERE user_id=? AND subject_id=? ORDER BY end_at",
        ("compact", "BIO"),
    )
    assert len(rows) >= 2
    first_ids = set(db.loads(rows[0]["source_session_ids_json"], []))
    second_ids = set(db.loads(rows[1]["source_session_ids_json"], []))
    assert first_ids.isdisjoint(second_ids)


def test_distinct_admin_key_can_pass_general_middleware(settings):
    secured = replace(
        settings,
        debug=False,
        api_key="general-access-key-123456",
        admin_api_key="admin-access-key-12345678",
    )
    app = create_app(secured)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/subjects").status_code == 401
        assert client.get(
            "/subjects", headers={"Authorization": "Bearer general-access-key-123456"}
        ).status_code == 200
        # The admin credential must survive middleware and satisfy route-level
        # authorization even when it is deliberately different from the API key.
        response = client.post(
            "/subjects/packs",
            headers={"Authorization": "Bearer admin-access-key-12345678"},
            json={
                "subject_id": "AUTHMATH",
                "name": "Auth Math",
                "concepts": [{"concept_id": "auth_math_c1", "name": "Concept"}],
                "edges": [],
                "questions": [],
            },
        )
        assert response.status_code == 200


def test_ungraded_free_text_cannot_poison_learner_state(core, settings):
    db, retrieval, memory, learner = core
    # No deterministic rubric. Seeded trusted Biology context exists, but no
    # supervisor is configured, so the response must remain ungraded/zero-weight.
    db.execute(
        """
        INSERT INTO questions(
          question_id,subject_id,primary_concept_id,secondary_concept_ids_json,
          stem,answer_type,answer_key_json,difficulty,is_diagnostic,
          diagnostic_model_json,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "bio_ungraded_free_text", "BIO", "bio_golgi", "[]",
            "Explain the Golgi apparatus.", "short", "{}", 0.5, 0, "{}", "{}",
        ),
    )
    theory = TheoryService(db, retrieval, ModelGateway(settings))
    session = memory.start_session(user_id="ungraded", subject_id="BIO", goal=None, source="text")
    evaluation, event = theory.evaluate(
        TheoryAnswerRequest(
            session_id=session.session_id,
            user_id="ungraded",
            question_id="bio_ungraded_free_text",
            answer="I am not sure.",
        )
    )
    assert evaluation.correct is None
    assert evaluation.evidence_strength == 0.0
    learner.process_event(event)
    assert memory.get_concept_state("ungraded", "bio_golgi") is None


def test_prerequisite_cycle_is_rejected_transactionally(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/subjects/packs",
            json={
                "subject_id": "CYCLE",
                "name": "Cycle Subject",
                "concepts": [
                    {"concept_id": "cycle_a", "name": "A"},
                    {"concept_id": "cycle_b", "name": "B"},
                ],
                "edges": [
                    {"source_concept_id": "cycle_a", "target_concept_id": "cycle_b", "relation": "PREREQUISITE_OF"},
                    {"source_concept_id": "cycle_b", "target_concept_id": "cycle_a", "relation": "PREREQUISITE_OF"},
                ],
                "questions": [],
            },
        )
        assert response.status_code == 400
        # Transaction rollback means even the subject row was not partially written.
        assert app.state.services.db.fetchone(
            "SELECT 1 FROM subjects WHERE subject_id='CYCLE'"
        ) is None


def test_duplicate_and_future_events_are_rejected_before_state_update(core):
    db, retrieval, memory, learner = core
    session = memory.start_session(user_id="ledger", subject_id="BIO", goal=None, source="text")
    event = EvidenceEvent(
        event_id="stable-event-id",
        session_id=session.session_id,
        user_id="ledger",
        subject_id="BIO",
        concept_ids=["bio_golgi"],
        primary_concept_id="bio_golgi",
        activity_type="answer",
        correct=False,
    )
    learner.process_event(event)
    before = memory.get_concept_state("ledger", "bio_golgi")
    assert before is not None
    with pytest.raises(ValueError, match="already been recorded"):
        learner.process_event(event)
    after = memory.get_concept_state("ledger", "bio_golgi")
    assert after is not None and after.evidence_count == before.evidence_count

    future = EvidenceEvent(
        session_id=session.session_id,
        user_id="ledger",
        subject_id="BIO",
        concept_ids=["bio_golgi"],
        primary_concept_id="bio_golgi",
        activity_type="answer",
        correct=True,
        timestamp=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    with pytest.raises(ValueError, match="future"):
        learner.process_event(future)


def test_recovered_mastery_does_not_manufacture_a_gap(core):
    db, retrieval, memory, learner = core
    session = memory.start_session(user_id="mastered", subject_id="BIO", goal=None, source="text")
    for i in range(24):
        learner.process_event(EvidenceEvent(
            session_id=session.session_id,
            user_id="mastered",
            subject_id="BIO",
            concept_ids=["bio_golgi"],
            primary_concept_id="bio_golgi",
            activity_type=f"practice_{i}",
            correct=True,
        ))
    state = memory.get_concept_state("mastered", "bio_golgi")
    assert state is not None and state.mastery_belief >= learner.RECOVERED_MASTERY
    diagnosis = learner.diagnose("mastered", "bio_golgi")
    assert diagnosis.confident is False
    assert diagnosis.recommended_action == "practice"


def test_partial_delete_invalidates_gap_rows_that_reference_affected_gap(core):
    db, retrieval, memory, learner = core
    session = memory.start_session(user_id="gap-delete", subject_id="DSA", goal=None, source="text")
    event = EvidenceEvent(
        session_id=session.session_id,
        user_id="gap-delete",
        subject_id="DSA",
        concept_ids=["binary_search_boundary"],
        primary_concept_id="binary_search_boundary",
        activity_type="diagnostic",
        correct=False,
    )
    learner.process_event(event)
    db.execute(
        """
        INSERT OR REPLACE INTO gap_hypotheses(
          user_id,target_concept_id,gap_concept_id,probability,source,evidence_json,updated_at
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            "gap-delete", "binary_search", "binary_search_boundary", 0.9,
            "prerequisite", "[]", datetime.now(timezone.utc).isoformat(),
        ),
    )
    memory.delete_memory(
        user_id="gap-delete",
        start_at=event.timestamp - timedelta(seconds=1),
        end_at=event.timestamp + timedelta(seconds=1),
    )
    assert db.fetchone(
        """
        SELECT 1 FROM gap_hypotheses
        WHERE user_id=? AND target_concept_id=? AND gap_concept_id=?
        """,
        ("gap-delete", "binary_search", "binary_search_boundary"),
    ) is None


def test_session_summary_tracks_teaching_interventions(core):
    db, retrieval, memory, learner = core
    session = memory.start_session(user_id="summary-teach", subject_id="BIO", goal="review", source="text")
    memory.record_intervention(
        session_id=session.session_id,
        user_id="summary-teach",
        subject_id="BIO",
        concept_id="bio_golgi",
        action_type="contrastive_explanation",
        content="verified explanation",
        grounded=True,
        source_ids=["demo-bio-organelle-pack"],
        supervisor_used=False,
    )
    summary = memory.complete_session(session.session_id)
    assert summary["intervention_count"] == 1
    assert summary["intervention_actions"]["contrastive_explanation"] == 1
    assert "bio_golgi" in summary["concepts"]


def test_partial_delete_rebuilds_compaction_without_deleted_session(core):
    db, retrieval, memory, learner = core
    event_by_session = {}
    for i in range(15):
        session = memory.start_session(
            user_id="compact-delete", subject_id="BIO", goal=f"s{i}", source="text"
        )
        event = EvidenceEvent(
            session_id=session.session_id,
            user_id="compact-delete",
            subject_id="BIO",
            concept_ids=["bio_golgi"],
            primary_concept_id="bio_golgi",
            activity_type="answer",
            correct=bool(i % 2),
        )
        learner.process_event(event)
        memory.complete_session(session.session_id)
        event_by_session[session.session_id] = event

    first_compact = db.fetchone(
        """
        SELECT source_session_ids_json FROM compacted_history
        WHERE user_id=? AND subject_id=? ORDER BY end_at LIMIT 1
        """,
        ("compact-delete", "BIO"),
    )
    assert first_compact is not None
    source_ids = db.loads(first_compact["source_session_ids_json"], [])
    victim_session = source_ids[0]
    victim_event = event_by_session[victim_session]

    memory.delete_memory(
        user_id="compact-delete",
        start_at=victim_event.timestamp - timedelta(microseconds=1),
        end_at=victim_event.timestamp + timedelta(microseconds=1),
    )
    rebuilt = db.fetchall(
        "SELECT source_session_ids_json FROM compacted_history WHERE user_id=? AND subject_id=?",
        ("compact-delete", "BIO"),
    )
    all_sources = {
        sid
        for row in rebuilt
        for sid in db.loads(row["source_session_ids_json"], [])
    }
    assert victim_session not in all_sources


def test_static_dsa_pattern_does_not_contaminate_unrelated_topic(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        session = client.post(
            "/sessions", json={"user_id": "scope-code", "subject_id": "DSA", "source": "code"}
        ).json()
        response = client.post(
            "/code/analyze?run_tests=false&record=false",
            json={
                "session_id": session["session_id"],
                "user_id": "scope-code",
                "subject_id": "DSA",
                "concept_id": "recursion",
                "code": "def f(x):\n    low = mid\n    return x\n",
            },
        )
        assert response.status_code == 200
        assert "binary_search_boundary" not in response.json()["candidate_concepts"]


def test_unevaluated_interaction_does_not_inflate_mastery_confidence(core):
    db, retrieval, memory, learner = core
    session = memory.start_session(user_id="no-outcome", subject_id="DSA", goal=None, source="code")
    event = EvidenceEvent(
        session_id=session.session_id,
        user_id="no-outcome",
        subject_id="DSA",
        concept_ids=["binary_search"],
        primary_concept_id="binary_search",
        activity_type="static_analysis_only",
        correct=None,
        score=None,
        evidence_strength=0.9,
        concept_likelihoods={"binary_search": 1.8},
    )
    learner.process_event(event)
    assert memory.get_concept_state("no-outcome", "binary_search") is None
    # Rebuild must preserve the same semantics rather than inventing confidence.
    learner.rebuild_user_state("no-outcome", {"binary_search"})
    assert memory.get_concept_state("no-outcome", "binary_search") is None


def test_database_initialize_backfills_partially_missing_fts_rows(core):
    db, retrieval, memory, learner = core
    if not db.fts_available:
        pytest.skip("SQLite build has no FTS5")
    retrieval.upsert_chunk(KnowledgeChunkIn(
        chunk_id="fts-backfill-old", source_id="audit", subject_id="BIO",
        concept_ids=["bio_peroxisome"], content_type="definition",
        content="uniquezymex catalase peroxisome legacy chunk",
    ))
    # Simulate a database that has some FTS rows, but this older row is missing.
    db.execute("DELETE FROM knowledge_fts WHERE chunk_id=?", ("fts-backfill-old",))
    assert db.fetchone("SELECT 1 FROM knowledge_fts LIMIT 1") is not None
    db.initialize()
    assert db.fetchone(
        "SELECT 1 FROM knowledge_fts WHERE chunk_id=?", ("fts-backfill-old",)
    ) is not None
    hits = retrieval.search(
        query="uniquezymex", subject_id="BIO", concept_ids=["bio_peroxisome"], limit=2
    )
    assert hits and hits[0].chunk_id == "fts-backfill-old"


def test_hints_reduce_bkt_mastery_evidence_as_well_as_adaptive_evidence(core):
    db, retrieval, memory, learner = core
    s1 = memory.start_session(user_id="bkt-independent", subject_id="DSA", goal=None, source="text")
    s2 = memory.start_session(user_id="bkt-supported", subject_id="DSA", goal=None, source="text")
    independent = EvidenceEvent(
        session_id=s1.session_id, user_id="bkt-independent", subject_id="DSA",
        concept_ids=["binary_search"], primary_concept_id="binary_search",
        activity_type="answer", correct=True,
        metadata={"behavior": {"hint_count": 0, "attempt_count": 1}},
    )
    supported = EvidenceEvent(
        session_id=s2.session_id, user_id="bkt-supported", subject_id="DSA",
        concept_ids=["binary_search"], primary_concept_id="binary_search",
        activity_type="answer", correct=True,
        metadata={"behavior": {"hint_count": 2, "attempt_count": 1}},
    )
    learner.process_event(independent)
    learner.process_event(supported)
    a = memory.get_concept_state("bkt-independent", "binary_search")
    b = memory.get_concept_state("bkt-supported", "binary_search")
    assert a is not None and b is not None
    assert a.mastery_belief > b.mastery_belief


def test_admin_key_alone_can_secure_remote_capable_instance(settings):
    secured = replace(
        settings,
        debug=False,
        api_key=None,
        admin_api_key="admin-only-key-123456789",
    )
    app = create_app(secured)
    with TestClient(app) as client:
        assert client.get("/subjects").status_code == 401
        assert client.get(
            "/subjects", headers={"Authorization": "Bearer admin-only-key-123456789"}
        ).status_code == 200


def test_curriculum_bkt_change_replays_existing_learner_state(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        session = client.post(
            "/sessions", json={"user_id": "replay-user", "subject_id": "DSA", "source": "text"}
        ).json()
        event = {
            "session_id": session["session_id"],
            "user_id": "replay-user",
            "subject_id": "DSA",
            "concept_ids": ["binary_search"],
            "primary_concept_id": "binary_search",
            "activity_type": "answer",
            "correct": True,
        }
        assert client.post("/events", json=event).status_code == 200
        before = app.state.services.memory.get_concept_state("replay-user", "binary_search")
        assert before is not None
        response = client.post(
            "/subjects/packs",
            json={
                "subject_id": "DSA",
                "name": "Data Structures & Algorithms",
                "concepts": [{
                    "concept_id": "binary_search",
                    "name": "Binary Search",
                    "bkt_params": {"prior": 0.8, "slip": 0.05, "guess": 0.1, "learn": 0.05},
                }],
                "edges": [],
                "questions": [],
            },
        )
        assert response.status_code == 200
        assert response.json()["replayed_users"] == 1
        after = app.state.services.memory.get_concept_state("replay-user", "binary_search")
        assert after is not None
        assert after.mastery_belief != before.mastery_belief
