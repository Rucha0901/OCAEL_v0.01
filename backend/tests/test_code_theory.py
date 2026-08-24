from __future__ import annotations

from adaptive_backend.subjects.dsa import CodeReviewService
from adaptive_backend.models import ModelGateway
from adaptive_backend.schemas import CodeAnalysisRequest, TheoryAnswerRequest
from adaptive_backend.subjects.biology import TheoryService


def test_code_reviewer_detects_boundary_pattern(core, settings):
    db, retrieval, memory, learner = core
    service = CodeReviewService(db, settings)
    s = memory.start_session(user_id="u4", subject_id="DSA", goal=None, source="code")
    result = service.analyze(
        CodeAnalysisRequest(
            session_id=s.session_id,
            user_id="u4",
            concept_id="binary_search",
            code="""
def binary_search(arr, target):
    low, high = 0, len(arr)-1
    while low < high:
        mid = (low + high)//2
        if arr[mid] < target:
            low = mid
        else:
            high = mid
    return low if arr and arr[low] == target else -1
""",
            problem_id="dsa_problem_binary_search",
        )
    )
    assert result.compile_ok
    assert "low_assigned_mid_without_visible_increment" in result.static_patterns
    assert result.candidate_concepts["binary_search_boundary"] > 1.0


def test_theory_mcq_creates_misconception_evidence(core, settings):
    db, retrieval, memory, learner = core
    models = ModelGateway(settings)
    service = TheoryService(db, retrieval, models)
    s = memory.start_session(user_id="u5", subject_id="BIO", goal=None, source="text")
    evaluation, event = service.evaluate(
        TheoryAnswerRequest(
            session_id=s.session_id,
            user_id="u5",
            question_id="bio_q_golgi_packaging",
            answer="RER",
        )
    )
    assert evaluation.correct is False
    assert event.mistake_type == "rer_golgi_function_confusion"
    assert event.concept_likelihoods["bio_golgi"] > 1.0


def test_short_answer_rubric_rejects_vague_protein_only(core, settings):
    db, retrieval, memory, learner = core
    service = TheoryService(db, retrieval, ModelGateway(settings))
    s = memory.start_session(user_id="rubric", subject_id="BIO", goal=None, source="text")
    evaluation, _ = service.evaluate(
        TheoryAnswerRequest(
            session_id=s.session_id,
            user_id="rubric",
            question_id="bio_q_rer_role",
            answer="protein",
        )
    )
    assert evaluation.correct is False
    assert evaluation.score < 1.0
