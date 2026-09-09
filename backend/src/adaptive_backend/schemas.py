from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class HealthResponse(StrictModel):
    status: str = "ok"
    version: str
    database: str
    tutor_api_configured: bool
    local_model_configured: bool
    supervisor_configured: bool


class SessionStart(StrictModel):
    user_id: str = Field(default="local", min_length=1, max_length=128)
    subject_id: str = Field(pattern=ID_PATTERN)
    goal: str | None = Field(default=None, max_length=1000)
    source: Literal["text", "voice", "ocr", "code", "system"] = "text"


class SessionRecord(StrictModel):
    session_id: str
    user_id: str
    subject_id: str
    started_at: datetime
    ended_at: datetime | None = None
    status: Literal["active", "completed", "abandoned"] = "active"
    goal: str | None = None


class EvidenceEvent(StrictModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(default="local", min_length=1, max_length=128)
    subject_id: str = Field(pattern=ID_PATTERN)
    concept_ids: list[str] = Field(min_length=1, max_length=32)
    primary_concept_id: str = Field(pattern=ID_PATTERN)
    timestamp: datetime = Field(default_factory=utc_now)
    activity_type: str = Field(min_length=1, max_length=128)
    input_mode: Literal["text", "voice", "ocr", "code", "system"] = "text"
    correct: bool | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_strength: float = Field(default=1.0, ge=0.0, le=1.0)
    response_text: str | None = Field(default=None, max_length=20_000)
    mistake_type: str | None = Field(default=None, max_length=128)
    concept_likelihoods: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("concept_ids")
    @classmethod
    def _require_concepts(cls, value: list[str]) -> list[str]:
        clean = list(dict.fromkeys(value))
        for concept_id in clean:
            if not concept_id or len(concept_id) > 128:
                raise ValueError("concept_ids contains an invalid concept ID")
        return clean

    @field_validator("concept_likelihoods")
    @classmethod
    def _likelihood_ratios_are_bounded(cls, value: dict[str, float]) -> dict[str, float]:
        if len(value) > 32:
            raise ValueError("concept_likelihoods contains too many entries")
        for concept_id, factor in value.items():
            if not concept_id or len(concept_id) > 128:
                raise ValueError("concept_likelihoods contains an invalid concept ID")
            if factor <= 0.0 or factor > 20.0:
                raise ValueError("concept likelihood factors must be in (0, 20]")
        return value

    @field_validator("timestamp")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _primary_must_be_in_concepts(self) -> "EvidenceEvent":
        if self.primary_concept_id not in self.concept_ids:
            raise ValueError("primary_concept_id must be present in concept_ids")
        return self


class ConceptState(StrictModel):
    user_id: str
    concept_id: str
    mastery_belief: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_count: int = Field(ge=0)
    last_observed_at: datetime | None = None
    status: Literal[
        "unknown",
        "developing",
        "verified",
        "needs_review",
        "active_blocker",
        "recovered",
        "stale",
    ] = "unknown"
    recurrence_count: int = Field(default=0, ge=0)


class GapHypothesis(StrictModel):
    concept_id: str
    probability: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list, max_length=50)
    source: Literal["prerequisite", "misconception", "component", "direct"] = "direct"


class DiagnosisResult(StrictModel):
    target_concept_id: str
    hypotheses: list[GapHypothesis]
    entropy: float = Field(ge=0.0)
    confident: bool
    confidence_margin: float = Field(ge=0.0, le=1.0)
    recommended_action: Literal[
        "ask_diagnostic", "teach", "practice", "reassess", "insufficient_evidence"
    ]
    selected_question_id: str | None = None


class QuestionRecord(StrictModel):
    question_id: str
    subject_id: str
    primary_concept_id: str
    secondary_concept_ids: list[str] = Field(default_factory=list)
    stem: str
    answer_type: Literal["mcq", "short", "code", "boolean"]
    answer_key: Any
    difficulty: float = Field(default=0.5, ge=0.0, le=1.0)
    is_diagnostic: bool = False
    diagnostic_model: dict[str, dict[str, float]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResponseBehavior(StrictModel):
    """Optional evidence describing how an answer was produced.

    Timing is a pedagogical signal only; it is never used directly as an
    intelligence or mastery proxy.
    """

    response_seconds: float | None = Field(default=None, ge=0.0, le=86_400)
    expected_seconds: float | None = Field(default=None, gt=0.0, le=86_400)
    hint_count: int = Field(default=0, ge=0, le=100)
    attempt_count: int = Field(default=1, ge=1, le=100)
    self_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evaluator_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class AdaptiveState(StrictModel):
    user_id: str
    subject_id: str
    concept_id: str
    theta: float
    variance: float = Field(gt=0.0)
    target_difficulty: float = Field(ge=0.0, le=1.0)
    difficulty_band: Literal["easy", "medium", "hard"]
    observations: int = Field(ge=0)
    micro_stage_position: int = Field(ge=0)
    behavior_profile: str


class AdaptiveDecision(StrictModel):
    mode: Literal["diagnostic", "practice"]
    target_concept_id: str
    selected_question_id: str | None = None
    target_difficulty: float = Field(ge=0.0, le=1.0)
    difficulty_band: Literal["easy", "medium", "hard"]
    theta: float
    uncertainty: float = Field(ge=0.0)
    expected_success: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str
    behavior_profile: str
    micro_stage_position: int = Field(ge=0)
    micro_stage_size: int = Field(ge=1)


class AdaptiveNextRequest(StrictModel):
    user_id: str = Field(default="local", min_length=1, max_length=128)
    subject_id: str = Field(pattern=ID_PATTERN)
    concept_id: str = Field(pattern=ID_PATTERN)


class TheoryAnswerRequest(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(default="local", min_length=1, max_length=128)
    question_id: str = Field(pattern=ID_PATTERN)
    answer: str = Field(max_length=20_000)
    input_mode: Literal["text", "voice", "ocr"] = "text"
    behavior: ResponseBehavior | None = None


class TheoryEvaluation(StrictModel):
    correct: bool | None
    score: float = Field(ge=0.0, le=1.0)
    evidence_strength: float = Field(ge=0.0, le=1.0)
    feedback: str
    mistake_type: str | None = None
    concept_likelihoods: dict[str, float] = Field(default_factory=dict)
    grounded: bool = False
    source_ids: list[str] = Field(default_factory=list)


class CodeAnalysisRequest(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(default="local", min_length=1, max_length=128)
    subject_id: str = Field(default="DSA", pattern=ID_PATTERN)
    concept_id: str = Field(pattern=ID_PATTERN)
    language: Literal["python"] = "python"
    code: str = Field(max_length=200_000)
    problem_id: str | None = Field(default=None, pattern=ID_PATTERN)
    behavior: ResponseBehavior | None = None


class CodeAnalysisResult(StrictModel):
    compile_ok: bool
    syntax_error: str | None = None
    static_patterns: list[str] = Field(default_factory=list)
    candidate_concepts: dict[str, float] = Field(default_factory=dict)
    complexity_notes: list[str] = Field(default_factory=list)
    test_summary: dict[str, Any] = Field(default_factory=dict)
    safe_execution_used: bool = False


class TutorRequest(StrictModel):
    user_id: str = Field(default="local", min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    subject_id: str = Field(pattern=ID_PATTERN)
    target_concept_id: str = Field(pattern=ID_PATTERN)
    user_message: str = Field(max_length=20_000)
    action: Literal[
        "explain",
        "hint",
        "analogy",
        "worked_example",
        "socratic_question",
        "practice",
        "reassess",
        "prerequisite_rewind",
        "summary",
    ] = "explain"
    force_supervisor: bool = False


class TutorResponse(StrictModel):
    message: str
    action: str
    grounded: bool
    citations: list[str] = Field(default_factory=list)
    supervisor_used: bool = False
    next_check: str | None = None
    diagnosis: DiagnosisResult | None = None


class KnowledgeChunkIn(StrictModel):
    chunk_id: str | None = Field(default=None, pattern=ID_PATTERN)
    source_id: str = Field(min_length=1, max_length=256)
    subject_id: str = Field(pattern=ID_PATTERN)
    unit: str | None = Field(default=None, max_length=256)
    topic: str | None = Field(default=None, max_length=256)
    concept_ids: list[str] = Field(default_factory=list, max_length=64)
    prerequisite_ids: list[str] = Field(default_factory=list, max_length=64)
    difficulty: float = Field(default=0.5, ge=0.0, le=1.0)
    content_type: str = Field(default="explanation", min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=100_000)
    citation_location: str | None = Field(default=None, max_length=1000)
    verified: bool = True
    version: str = Field(default="1", min_length=1, max_length=64)


class KnowledgeHit(StrictModel):
    chunk_id: str
    source_id: str
    subject_id: str
    content: str
    concept_ids: list[str]
    citation_location: str | None = None
    rank_score: float
    verified: bool


class KnowledgeSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    subject_id: str = Field(pattern=ID_PATTERN)
    concept_ids: list[str] = Field(default_factory=list, max_length=32)
    limit: int = Field(default=6, ge=1, le=20)


class SubjectConceptIn(StrictModel):
    concept_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=4000)
    bkt_params: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubjectEdgeIn(StrictModel):
    source_concept_id: str = Field(pattern=ID_PATTERN)
    target_concept_id: str = Field(pattern=ID_PATTERN)
    relation: str = Field(min_length=1, max_length=64)
    strength: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubjectQuestionIn(StrictModel):
    question_id: str = Field(pattern=ID_PATTERN)
    primary_concept_id: str = Field(pattern=ID_PATTERN)
    secondary_concept_ids: list[str] = Field(default_factory=list, max_length=32)
    stem: str = Field(min_length=1, max_length=20_000)
    answer_type: Literal["mcq", "short", "code", "boolean"]
    answer_key: Any
    difficulty: float = Field(default=0.5, ge=0.0, le=1.0)
    is_diagnostic: bool = False
    diagnostic_model: dict[str, dict[str, float]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubjectPackIn(StrictModel):
    subject_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=4000)
    concepts: list[SubjectConceptIn] = Field(min_length=1, max_length=2000)
    edges: list[SubjectEdgeIn] = Field(default_factory=list, max_length=10_000)
    questions: list[SubjectQuestionIn] = Field(default_factory=list, max_length=10_000)


class MemoryMapNode(StrictModel):
    id: str
    label: str
    kind: str
    status: str = "unknown"
    subject_id: str | None = None


class MemoryMapEdge(StrictModel):
    source: str
    target: str
    relation: str


class LearningMap(StrictModel):
    nodes: list[MemoryMapNode]
    edges: list[MemoryMapEdge]


class MemoryDeleteRequest(StrictModel):
    user_id: str = Field(default="local", min_length=1, max_length=128)
    subject_id: str | None = Field(default=None, pattern=ID_PATTERN)
    start_at: datetime | None = None
    end_at: datetime | None = None
    delete_all: bool = False

    @field_validator("start_at", "end_at")
    @classmethod
    def _normalize_range_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _valid_scope(self) -> "MemoryDeleteRequest":
        if self.start_at and self.end_at and self.start_at > self.end_at:
            raise ValueError("start_at must be <= end_at")
        if self.delete_all and any((self.subject_id, self.start_at, self.end_at)):
            raise ValueError("delete_all cannot be combined with subject/date filters")
        if not self.delete_all and not any((self.subject_id, self.start_at, self.end_at)):
            raise ValueError("Specify subject/date filters or delete_all=true")
        return self


class OCRResult(StrictModel):
    text: str
    engine: str
    confidence: float | None = None


class SpeechTranscript(StrictModel):
    text: str
    engine: str
    language: str | None = None


class EvaluationCase(StrictModel):
    case_id: str = Field(min_length=1, max_length=128)
    subject_id: str = Field(pattern=ID_PATTERN)
    target_concept_id: str = Field(pattern=ID_PATTERN)
    events: list[EvidenceEvent] = Field(max_length=500)
    expected_top_gap: str | None = None
    expected_action: str | None = None
