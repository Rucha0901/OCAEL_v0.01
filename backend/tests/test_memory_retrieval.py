from __future__ import annotations

from adaptive_backend.schemas import EvidenceEvent, KnowledgeChunkIn


def test_subject_scoped_map_and_retrieval(core):
    db, retrieval, memory, learner = core
    map_bio = memory.learning_map("u", "BIO")
    assert map_bio.nodes
    assert all(n.subject_id == "BIO" for n in map_bio.nodes)
    assert not any(n.id.startswith("binary_") for n in map_bio.nodes)

    hits = retrieval.search(
        query="packages proteins",
        subject_id="BIO",
        concept_ids=["bio_golgi"],
        limit=3,
    )
    assert hits
    assert hits[0].subject_id == "BIO"
    assert any("Golgi" in h.content for h in hits)


def test_privacy_delete_rebuilds_remaining_state(core):
    db, retrieval, memory, learner = core
    s = memory.start_session(user_id="u3", subject_id="BIO", goal=None, source="text")
    e1 = EvidenceEvent(
        session_id=s.session_id,
        user_id="u3",
        subject_id="BIO",
        concept_ids=["bio_golgi"],
        primary_concept_id="bio_golgi",
        activity_type="answer_mcq",
        correct=False,
    )
    learner.process_event(e1)
    e2 = EvidenceEvent(
        session_id=s.session_id,
        user_id="u3",
        subject_id="BIO",
        concept_ids=["bio_rer"],
        primary_concept_id="bio_rer",
        activity_type="answer_short",
        correct=True,
    )
    learner.process_event(e2)
    assert memory.get_concept_state("u3", "bio_golgi") is not None
    assert memory.get_concept_state("u3", "bio_rer") is not None

    affected = memory.delete_memory(user_id="u3", subject_id="BIO")
    assert "bio_golgi" in affected
    assert not memory.recent_events(user_id="u3", subject_id="BIO")


def test_paused_personalization_does_not_store_intervention(core):
    db, retrieval, memory, learner = core
    s = memory.start_session(user_id="no-store", subject_id="BIO", goal=None, source="text")
    memory.set_personalization("no-store", False)
    intervention_id = memory.record_intervention(
        session_id=s.session_id,
        user_id="no-store",
        subject_id="BIO",
        concept_id="bio_golgi",
        action_type="explain",
        content="temporary response",
        grounded=True,
        source_ids=["demo"],
        supervisor_used=False,
    )
    assert intervention_id == ""
    row = db.fetchone("SELECT count(*) AS n FROM interventions WHERE user_id=?", ("no-store",))
    assert row["n"] == 0
