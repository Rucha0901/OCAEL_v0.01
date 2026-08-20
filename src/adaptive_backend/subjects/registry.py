from __future__ import annotations

from typing import Any

from ..database import Database
from ..schemas import SubjectPackIn


class SubjectRegistry:
    """Transactional, ownership-safe subject-pack registry.

    Concept/question identifiers are global keys in the physical schema. A pack
    therefore may update identifiers already owned by the same subject, but it
    may never silently steal them from another subject.
    """

    def __init__(self, db: Database):
        self.db = db

    def list_subjects(self) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT s.subject_id,s.name,s.description,
                   count(DISTINCT c.concept_id) AS concept_count,
                   count(DISTINCT q.question_id) AS question_count
            FROM subjects s
            LEFT JOIN concepts c ON c.subject_id=s.subject_id
            LEFT JOIN questions q ON q.subject_id=s.subject_id
            GROUP BY s.subject_id,s.name,s.description
            ORDER BY s.name
            """
        )
        return [dict(row) for row in rows]

    def _validate_prerequisite_acyclic(self, pack: SubjectPackIn, known: set[str], conn: Any) -> None:
        rows = conn.execute(
            """
            SELECT e.source_concept_id,e.target_concept_id
            FROM concept_edges e
            JOIN concepts s ON s.concept_id=e.source_concept_id
            JOIN concepts t ON t.concept_id=e.target_concept_id
            WHERE e.relation='PREREQUISITE_OF' AND s.subject_id=? AND t.subject_id=?
            """,
            (pack.subject_id, pack.subject_id),
        ).fetchall()
        graph: dict[str, set[str]] = {cid: set() for cid in known}
        for row in rows:
            graph.setdefault(row["source_concept_id"], set()).add(row["target_concept_id"])
        # Incoming edges overwrite same triples but otherwise augment the pack.
        for edge in pack.edges:
            if edge.relation == "PREREQUISITE_OF":
                if edge.source_concept_id == edge.target_concept_id:
                    raise ValueError("A concept cannot be its own prerequisite")
                graph.setdefault(edge.source_concept_id, set()).add(edge.target_concept_id)

        visiting: set[str] = set()
        visited: set[str] = set()

        def dfs(node: str) -> None:
            if node in visiting:
                raise ValueError("PREREQUISITE_OF edges must form an acyclic graph")
            if node in visited:
                return
            visiting.add(node)
            for nxt in graph.get(node, ()):
                dfs(nxt)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            dfs(node)

    def upsert_pack(self, pack: SubjectPackIn) -> dict[str, Any]:
        # Validate purely local pack semantics before acquiring the write lock.
        from ..learning import BKTParams

        concept_ids = [c.concept_id for c in pack.concepts]
        if len(concept_ids) != len(set(concept_ids)):
            raise ValueError("Duplicate concept_id inside subject pack")
        question_ids = [q.question_id for q in pack.questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("Duplicate question_id inside subject pack")
        for concept in pack.concepts:
            BKTParams.from_mapping(concept.bkt_params)

        # Ownership checks, graph validation and writes share one BEGIN IMMEDIATE
        # transaction. This closes a TOCTOU race where two concurrent admin
        # requests could both observe an unused global ID and then mutate it.
        with self.db.transaction() as conn:
            for concept_id in concept_ids:
                owner = conn.execute(
                    "SELECT subject_id FROM concepts WHERE concept_id=?", (concept_id,)
                ).fetchone()
                if owner and owner["subject_id"] != pack.subject_id:
                    raise ValueError(
                        f"Concept ID {concept_id!r} is already owned by subject {owner['subject_id']!r}"
                    )
            for question_id in question_ids:
                owner = conn.execute(
                    "SELECT subject_id FROM questions WHERE question_id=?", (question_id,)
                ).fetchone()
                if owner and owner["subject_id"] != pack.subject_id:
                    raise ValueError(
                        f"Question ID {question_id!r} is already owned by subject {owner['subject_id']!r}"
                    )

            known = set(concept_ids)
            known.update(
                row["concept_id"]
                for row in conn.execute(
                    "SELECT concept_id FROM concepts WHERE subject_id=?", (pack.subject_id,)
                ).fetchall()
            )
            for edge in pack.edges:
                if edge.source_concept_id not in known or edge.target_concept_id not in known:
                    raise ValueError("Every subject edge must reference a concept owned by this subject")
            for question in pack.questions:
                refs = {question.primary_concept_id, *question.secondary_concept_ids}
                if not refs.issubset(known):
                    raise ValueError("Every question concept must belong to the subject pack")
                for gap_id, model in question.diagnostic_model.items():
                    if gap_id not in known:
                        raise ValueError("Diagnostic model references a concept outside this subject")
                    if not isinstance(model, dict) or "p_correct" not in model:
                        raise ValueError("Each diagnostic hypothesis requires p_correct")
                    try:
                        p_correct = float(model["p_correct"])
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Diagnostic p_correct must be numeric") from exc
                    if not 0.0 <= p_correct <= 1.0:
                        raise ValueError("Diagnostic p_correct must be in [0,1]")

            self._validate_prerequisite_acyclic(pack, known, conn)

            conn.execute(
                """
                INSERT INTO subjects(subject_id,name,description) VALUES(?,?,?)
                ON CONFLICT(subject_id) DO UPDATE SET name=excluded.name,description=excluded.description
                """,
                (pack.subject_id, pack.name, pack.description),
            )
            for concept in pack.concepts:
                conn.execute(
                    """
                    INSERT INTO concepts(concept_id,subject_id,name,description,bkt_params_json,metadata_json)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(concept_id) DO UPDATE SET
                      name=excluded.name,description=excluded.description,
                      bkt_params_json=excluded.bkt_params_json,metadata_json=excluded.metadata_json
                    """,
                    (
                        concept.concept_id, pack.subject_id, concept.name, concept.description,
                        self.db.dumps(concept.bkt_params), self.db.dumps(concept.metadata),
                    ),
                )
            for edge in pack.edges:
                conn.execute(
                    """
                    INSERT INTO concept_edges(source_concept_id,target_concept_id,relation,strength,metadata_json)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(source_concept_id,target_concept_id,relation) DO UPDATE SET
                      strength=excluded.strength,metadata_json=excluded.metadata_json
                    """,
                    (
                        edge.source_concept_id, edge.target_concept_id, edge.relation,
                        edge.strength, self.db.dumps(edge.metadata),
                    ),
                )
            for question in pack.questions:
                conn.execute(
                    """
                    INSERT INTO questions(
                      question_id,subject_id,primary_concept_id,secondary_concept_ids_json,
                      stem,answer_type,answer_key_json,difficulty,is_diagnostic,
                      diagnostic_model_json,metadata_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(question_id) DO UPDATE SET
                      primary_concept_id=excluded.primary_concept_id,
                      secondary_concept_ids_json=excluded.secondary_concept_ids_json,
                      stem=excluded.stem,answer_type=excluded.answer_type,
                      answer_key_json=excluded.answer_key_json,difficulty=excluded.difficulty,
                      is_diagnostic=excluded.is_diagnostic,
                      diagnostic_model_json=excluded.diagnostic_model_json,
                      metadata_json=excluded.metadata_json
                    """,
                    (
                        question.question_id, pack.subject_id, question.primary_concept_id,
                        self.db.dumps(question.secondary_concept_ids), question.stem,
                        question.answer_type, self.db.dumps(question.answer_key),
                        question.difficulty, int(question.is_diagnostic),
                        self.db.dumps(question.diagnostic_model), self.db.dumps(question.metadata),
                    ),
                )

        return {
            "subject_id": pack.subject_id,
            "concepts": len(pack.concepts),
            "edges": len(pack.edges),
            "questions": len(pack.questions),
        }
