from __future__ import annotations

import re
from typing import Any

from ..database import Database
from ..models import ModelGateway, ModelUnavailable
from ..retrieval import RetrievalService
from ..schemas import EvidenceEvent, TheoryAnswerRequest, TheoryEvaluation


class TheoryService:
    """Theory-domain evaluator with deterministic grading first.

    MCQ/boolean/known short-answer rubrics are evaluated locally. The supervisor
    is only used for ambiguous free-text answers when configured.
    """

    def __init__(self, db: Database, retrieval: RetrievalService, models: ModelGateway):
        self.db = db
        self.retrieval = retrieval
        self.models = models

    def evaluate(self, request: TheoryAnswerRequest) -> tuple[TheoryEvaluation, EvidenceEvent]:
        session = self.db.fetchone("SELECT user_id,subject_id,status FROM sessions WHERE session_id=?", (request.session_id,))
        if not session or session["user_id"] != request.user_id or session["status"] != "active":
            raise ValueError("Theory answer requires a matching active session")
        row = self.db.fetchone("SELECT * FROM questions WHERE question_id=?", (request.question_id,))
        if not row:
            raise ValueError("Unknown question_id")

        subject_id = row["subject_id"]
        if session["subject_id"] != subject_id:
            raise ValueError("Question does not belong to the active session subject")
        primary = row["primary_concept_id"]
        secondary = self.db.loads(row["secondary_concept_ids_json"], [])
        answer_type = row["answer_type"]
        answer_key = self.db.loads(row["answer_key_json"], None)
        raw_metadata = self.db.loads(row["metadata_json"], {})
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

        if answer_type in {"mcq", "boolean"}:
            score, correct = self._exact_score(request.answer, answer_key)
            feedback = "Correct." if correct else "The response does not match the verified answer key."
            grounded = True
            raw_sources = metadata.get("source_ids", [])
            source_ids = [str(x) for x in raw_sources if isinstance(x, str)] if isinstance(raw_sources, list) else []
        elif answer_type == "short":
            score, correct, feedback, source_ids = self._short_answer(
                request.answer, answer_key, subject_id, primary, row["stem"], metadata
            )
            grounded = bool(source_ids) or bool(metadata.get("verified_rubric"))
        else:
            raise ValueError(f"Unsupported theory answer_type: {answer_type}")

        raw_map = metadata.get("misconception_map", {})
        misconception_map = raw_map if isinstance(raw_map, dict) else {}
        mistake_type = None
        factors: dict[str, float] = {}
        if correct is False:
            normalized = self._norm(request.answer)
            mapped = misconception_map.get(normalized) or misconception_map.get(request.answer.strip())
            if isinstance(mapped, dict):
                mistake_type = mapped.get("mistake_type")
                raw = mapped.get("concept_factors", {})
                if isinstance(raw, dict):
                    factors = {
                        str(k): float(v)
                        for k, v in raw.items()
                        if isinstance(v, (int, float)) and v > 0
                    }
            if not mistake_type:
                mistake_type = metadata.get("default_mistake_type") or "incorrect_theory_response"

        evaluation = TheoryEvaluation(
            correct=correct,
            score=score,
            evidence_strength=(
                1.0 if answer_type in {"mcq", "boolean"}
                else 0.0 if correct is None
                else max(0.45, score if correct else 0.65)
            ),
            feedback=feedback,
            mistake_type=mistake_type,
            concept_likelihoods=factors,
            grounded=grounded,
            source_ids=source_ids,
        )
        event = EvidenceEvent(
            session_id=request.session_id,
            user_id=request.user_id,
            subject_id=subject_id,
            concept_ids=[primary, *secondary],
            primary_concept_id=primary,
            activity_type=f"answer_{answer_type}",
            input_mode=request.input_mode,
            correct=correct,
            score=score,
            evidence_strength=evaluation.evidence_strength,
            response_text=request.answer,
            mistake_type=mistake_type,
            concept_likelihoods=factors,
            metadata={
                "question_id": request.question_id,
                "question_difficulty": float(row["difficulty"]),
                "irt_discrimination": self._safe_float(metadata.get("irt_discrimination", 1.0), 1.0),
                "source_ids": source_ids,
                "concept_strengths": {cid: 0.35 for cid in secondary},
                "behavior": request.behavior.model_dump(mode="json") if request.behavior else {},
            },
        )
        return evaluation, event

    @staticmethod
    def _norm(text: Any) -> str:
        return re.sub(r"\s+", " ", str(text).strip().lower())

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _source_ids(metadata: dict[str, Any]) -> list[str]:
        raw = metadata.get("source_ids", [])
        return [str(x) for x in raw if isinstance(x, str)] if isinstance(raw, list) else []

    def _exact_score(self, answer: str, answer_key: Any) -> tuple[float, bool]:
        if isinstance(answer_key, list):
            accepted = {self._norm(x) for x in answer_key}
        else:
            accepted = {self._norm(answer_key)}
        correct = self._norm(answer) in accepted
        return (1.0 if correct else 0.0), correct

    def _short_answer(
        self,
        answer: str,
        answer_key: Any,
        subject_id: str,
        primary_concept: str,
        question_stem: str,
        metadata: dict[str, Any],
    ) -> tuple[float, bool | None, str, list[str]]:
        if isinstance(answer_key, dict):
            key = answer_key
        elif isinstance(answer_key, list):
            key = {"acceptable_answers": answer_key}
        else:
            key = {"acceptable_answers": [answer_key]}
        acceptable = [self._norm(x) for x in key.get("acceptable_answers", []) if x is not None]
        normalized = self._norm(answer)
        if normalized in acceptable:
            return 1.0, True, "Correct.", self._source_ids(metadata)

        groups = key.get("required_groups", [])
        if groups:
            normalized_groups = [
                [self._norm(term) for term in group if term]
                for group in groups
                if isinstance(group, list) and group
            ]
            if normalized_groups:
                met = sum(1 for group in normalized_groups if any(term in normalized for term in group))
                score = met / len(normalized_groups)
                threshold = float(key.get("pass_threshold", 1.0))
                return (
                    score,
                    score >= threshold,
                    "The answer covers the required verified concepts."
                    if score >= threshold
                    else "The answer is missing one or more required concepts from the rubric.",
                    self._source_ids(metadata),
                )

        required = [self._norm(x) for x in key.get("required_terms", []) if x]
        if required:
            present = sum(1 for term in required if term in normalized)
            score = present / len(required)
            if score >= float(key.get("pass_threshold", 0.75)):
                return score, True, "The answer covers the required verified concepts.", self._source_ids(metadata)
            return score, False, "The answer is missing one or more required concepts from the rubric.", self._source_ids(metadata)

        # No deterministic rubric: retrieve trusted context and optionally ask the
        # higher-capability supervisor for a bounded semantic score.
        hits = self.retrieval.search(
            # Grade against the requested concept/question, not against semantic
            # neighbors of the student's possibly-wrong answer.
            query=question_stem,
            subject_id=subject_id,
            concept_ids=[primary_concept],
            limit=4,
            verified_only=True,
        )
        source_ids = [h.source_id for h in hits]
        if not hits:
            return 0.5, None, "No verified reference was found; answer left ungraded.", []
        if not self.models.supervisor_available:
            return 0.5, None, "Free-text answer requires a configured rubric or supervisor for reliable grading.", source_ids

        payload = {
            "task": "grade_theory_answer",
            "student_answer": answer,
            "verified_reference": [h.content for h in hits],
            "instructions": {
                "return_json": True,
                "fields": ["score", "correct", "feedback"],
                "score_range": [0, 1],
                "do_not_add_external_facts": True,
            },
        }
        try:
            result = self.models.supervisor_json(
                system=(
                    "Grade only against the supplied verified reference. Return JSON with "
                    "score (0..1), correct (boolean), and concise feedback. If the reference "
                    "is insufficient, set correct=false and explain insufficiency."
                ),
                payload=payload,
            )
            score = max(0.0, min(1.0, float(result.get("score", 0.0))))
            raw_correct = result.get("correct", None)
            if raw_correct is None:
                correct = score >= 0.75
            elif isinstance(raw_correct, bool):
                correct = raw_correct
            else:
                raise ValueError("Supervisor field 'correct' must be boolean")
            feedback = str(result.get("feedback", "Supervisor grading completed."))[:4000]
            return score, correct, feedback, source_ids
        except (ModelUnavailable, TypeError, ValueError):
            return 0.5, None, "Supervisor grading was unavailable; answer left unverified.", source_ids


def seed_biology(db: Database, retrieval: RetrievalService) -> None:
    """Seed a small, explicit demo pack. It is not presented as a full Biology KB."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subjects(subject_id,name,description) VALUES('BIO','Biology','Theory demo domain')"
        )
        concepts = [
            ("bio_cell", "Cell Biology", "Cell-level structures and functions"),
            ("bio_rer", "Rough Endoplasmic Reticulum", "Ribosome-associated protein synthesis and early processing"),
            ("bio_golgi", "Golgi Apparatus", "Modification, sorting and packaging of proteins and lipids"),
            ("bio_lysosome", "Lysosome", "Intracellular digestion and degradation"),
            ("bio_peroxisome", "Peroxisome", "Oxidative metabolism and hydrogen peroxide breakdown"),
        ]
        for cid, name, description in concepts:
            conn.execute(
                """
                INSERT OR IGNORE INTO concepts(concept_id,subject_id,name,description,bkt_params_json,metadata_json)
                VALUES(?, 'BIO', ?, ?, '{}', '{}')
                """,
                (cid, name, description),
            )
        edges = [
            ("bio_cell", "bio_rer", "PREREQUISITE_OF", 0.7),
            ("bio_cell", "bio_golgi", "PREREQUISITE_OF", 0.7),
            ("bio_rer", "bio_golgi", "COMMONLY_CONFUSED_WITH", 1.0),
            ("bio_lysosome", "bio_peroxisome", "COMMONLY_CONFUSED_WITH", 1.0),
        ]
        for src, dst, rel, strength in edges:
            conn.execute(
                """
                INSERT OR IGNORE INTO concept_edges(source_concept_id,target_concept_id,relation,strength)
                VALUES(?,?,?,?)
                """,
                (src, dst, rel, strength),
            )

        question_meta = db.dumps(
            {
                "source_ids": ["demo-bio-organelle-pack"],
                "verified_rubric": True,
                "misconception_map": {
                    "rough endoplasmic reticulum": {
                        "mistake_type": "rer_golgi_function_confusion",
                        "concept_factors": {"bio_rer": 4.0, "bio_golgi": 6.0},
                    },
                    "rer": {
                        "mistake_type": "rer_golgi_function_confusion",
                        "concept_factors": {"bio_rer": 4.0, "bio_golgi": 6.0},
                    },
                },
            }
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO questions(
              question_id,subject_id,primary_concept_id,secondary_concept_ids_json,
              stem,answer_type,answer_key_json,difficulty,is_diagnostic,
              diagnostic_model_json,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "bio_q_golgi_packaging",
                "BIO",
                "bio_golgi",
                db.dumps(["bio_rer"]),
                "Which organelle modifies, sorts, and packages proteins for transport?",
                "mcq",
                db.dumps(["golgi apparatus", "golgi"]),
                0.35,
                1,
                db.dumps(
                    {
                        "bio_golgi": {"p_correct": 0.20},
                        "bio_rer": {"p_correct": 0.45},
                        "bio_cell": {"p_correct": 0.75},
                    }
                ),
                question_meta,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO questions(
              question_id,subject_id,primary_concept_id,secondary_concept_ids_json,
              stem,answer_type,answer_key_json,difficulty,is_diagnostic,
              diagnostic_model_json,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "bio_q_rer_role",
                "BIO",
                "bio_rer",
                db.dumps(["bio_golgi"]),
                "State one major role of rough endoplasmic reticulum in protein handling.",
                "short",
                db.dumps(
                    {
                        "required_groups": [
                            ["protein", "proteins"],
                            ["synthesis", "synthesizes", "makes", "production"],
                        ],
                        "acceptable_answers": [
                            "protein synthesis",
                            "synthesis of proteins",
                            "protein synthesis and initial processing",
                        ],
                        "pass_threshold": 1.0,
                    }
                ),
                0.4,
                1,
                db.dumps(
                    {
                        "bio_rer": {"p_correct": 0.20},
                        "bio_golgi": {"p_correct": 0.60},
                        "bio_cell": {"p_correct": 0.80},
                    }
                ),
                db.dumps({"source_ids": ["demo-bio-organelle-pack"], "verified_rubric": True}),
            ),
        )

        practice_items = [
            (
                "bio_practice_golgi_easy", "bio_golgi", ["bio_rer"],
                "Which organelle is primarily responsible for sorting and packaging proteins for transport?",
                "mcq", ["golgi apparatus", "golgi"], 0.25,
            ),
            (
                "bio_practice_golgi_medium", "bio_golgi", ["bio_rer"],
                "After synthesis on rough ER, which organelle commonly modifies, sorts, and packages a secreted protein?",
                "short", ["golgi apparatus", "golgi"], 0.50,
            ),
            (
                "bio_practice_golgi_hard", "bio_golgi", ["bio_rer", "bio_cell"],
                "A newly synthesized secretory protein leaves rough ER in transport vesicles. Name the organelle that next performs major sorting and packaging for delivery.",
                "short", ["golgi apparatus", "golgi"], 0.78,
            ),
        ]
        for qid, primary_id, secondary_ids, stem, answer_type, answer_key, difficulty in practice_items:
            conn.execute(
                """
                INSERT OR IGNORE INTO questions(
                  question_id,subject_id,primary_concept_id,secondary_concept_ids_json,
                  stem,answer_type,answer_key_json,difficulty,is_diagnostic,
                  diagnostic_model_json,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    qid, "BIO", primary_id, db.dumps(secondary_ids), stem, answer_type,
                    db.dumps(answer_key), difficulty, 0, db.dumps({}),
                    db.dumps({
                        "source_ids": ["demo-bio-organelle-pack"],
                        "verified_rubric": True,
                        "irt_discrimination": 1.0,
                    }),
                ),
            )

    chunks = [
        {
            "chunk_id": "bio_chunk_rer",
            "source_id": "demo-bio-organelle-pack",
            "subject_id": "BIO",
            "topic": "Cell organelles",
            "concept_ids": ["bio_rer"],
            "content_type": "comparison",
            "content": "Rough endoplasmic reticulum is studded with ribosomes and is associated with synthesis and early processing of proteins entering the secretory pathway.",
            "citation_location": "demo pack: RER",
        },
        {
            "chunk_id": "bio_chunk_golgi",
            "source_id": "demo-bio-organelle-pack",
            "subject_id": "BIO",
            "topic": "Cell organelles",
            "concept_ids": ["bio_golgi", "bio_rer"],
            "content_type": "comparison",
            "content": "The Golgi apparatus modifies, sorts, and packages proteins and lipids received from the endoplasmic reticulum for delivery to other destinations.",
            "citation_location": "demo pack: Golgi",
        },
    ]
    from ..schemas import KnowledgeChunkIn

    for chunk in chunks:
        retrieval.upsert_chunk(KnowledgeChunkIn(**chunk))
