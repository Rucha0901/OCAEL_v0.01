from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .database import Database
from .schemas import KnowledgeChunkIn, KnowledgeHit


class RetrievalService:
    """Trusted corpus ingestion and constrained local retrieval.

    Retrieval is deliberately scoped *before* top-k ranking: subject and concept
    filters are SQL predicates, so a busy unrelated concept cannot crowd the
    requested concept out of the candidate set. FTS5 is optional; embedded SQLite
    builds without FTS fall back to deterministic lexical ranking.
    """

    def __init__(self, db: Database):
        self.db = db

    def upsert_chunk(self, chunk: KnowledgeChunkIn) -> str:
        chunk_id = chunk.chunk_id or str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            subject = conn.execute(
                "SELECT 1 FROM subjects WHERE subject_id=?", (chunk.subject_id,)
            ).fetchone()
            if not subject:
                raise ValueError(f"Unknown subject_id: {chunk.subject_id}")
            for concept_id in [*chunk.concept_ids, *chunk.prerequisite_ids]:
                row = conn.execute(
                    "SELECT subject_id FROM concepts WHERE concept_id=?", (concept_id,)
                ).fetchone()
                if not row:
                    raise ValueError(f"Unknown concept_id: {concept_id}")
                if row["subject_id"] != chunk.subject_id:
                    raise ValueError(
                        f"Knowledge concept {concept_id!r} does not belong to subject {chunk.subject_id!r}"
                    )

            existing = conn.execute(
                "SELECT subject_id FROM knowledge_chunks WHERE chunk_id=?", (chunk_id,)
            ).fetchone()
            if existing and existing["subject_id"] != chunk.subject_id:
                raise ValueError("A knowledge chunk ID cannot be moved between subjects")

            conn.execute(
                """
                INSERT INTO knowledge_chunks(
                  chunk_id,source_id,subject_id,unit,topic,concept_ids_json,
                  prerequisite_ids_json,difficulty,content_type,content,
                  citation_location,verified,version,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                  source_id=excluded.source_id,
                  unit=excluded.unit,
                  topic=excluded.topic,
                  concept_ids_json=excluded.concept_ids_json,
                  prerequisite_ids_json=excluded.prerequisite_ids_json,
                  difficulty=excluded.difficulty,
                  content_type=excluded.content_type,
                  content=excluded.content,
                  citation_location=excluded.citation_location,
                  verified=excluded.verified,
                  version=excluded.version
                """,
                (
                    chunk_id, chunk.source_id, chunk.subject_id, chunk.unit, chunk.topic,
                    self.db.dumps(chunk.concept_ids), self.db.dumps(chunk.prerequisite_ids),
                    chunk.difficulty, chunk.content_type, chunk.content,
                    chunk.citation_location, int(chunk.verified), chunk.version, now,
                ),
            )
        return chunk_id

    def _concept_clause(self, alias: str, concept_ids: list[str] | None) -> tuple[str, list[str]]:
        wanted = list(dict.fromkeys(concept_ids or []))
        if not wanted:
            return "", []
        # IDs are validated by the request schema. Parameters remain bound because
        # LIKE wildcards in an identifier must never become SQL syntax.
        clause = " AND (" + " OR ".join(f'{alias}.concept_ids_json LIKE ?' for _ in wanted) + ")"
        return clause, [f'%"{cid}"%' for cid in wanted]

    def _expand_concepts(self, subject_id: str, concept_ids: list[str] | None, *, depth: int = 1) -> list[str]:
        """Expand a small concept neighborhood for graph-guided retrieval.

        Expansion is deliberately shallow and subject-scoped so a prerequisite
        graph improves recall without turning every query into a whole-course dump.
        """
        seeds = list(dict.fromkeys(concept_ids or []))
        if not seeds or depth <= 0:
            return seeds
        seen = set(seeds)
        frontier = set(seeds)
        for _ in range(min(depth, 2)):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            rows = self.db.fetchall(
                f"""
                SELECT e.source_concept_id,e.target_concept_id,e.strength
                FROM concept_edges e
                JOIN concepts s ON s.concept_id=e.source_concept_id
                JOIN concepts t ON t.concept_id=e.target_concept_id
                WHERE s.subject_id=? AND t.subject_id=?
                  AND e.relation IN ('PREREQUISITE_OF','PART_OF','RELATED_TO')
                  AND (e.source_concept_id IN ({placeholders}) OR e.target_concept_id IN ({placeholders}))
                  AND e.strength>=0.45
                """,
                (subject_id, subject_id, *frontier, *frontier),
            )
            nxt: set[str] = set()
            for row in rows:
                for cid in (row["source_concept_id"], row["target_concept_id"]):
                    if cid not in seen:
                        seen.add(cid); nxt.add(cid)
            frontier = nxt
        return seeds + sorted(seen - set(seeds))

    def search(
        self,
        *,
        query: str,
        subject_id: str,
        concept_ids: list[str] | None = None,
        limit: int = 6,
        verified_only: bool = True,
        user_id: str | None = None,
        course_id: str | None = None,
    ) -> list[KnowledgeHit]:
        """Search global verified knowledge plus the caller's private material.

        `verified_only=True` means public/global material must be verified. A
        learner's own uploaded material is still eligible because it is explicitly
        owner-scoped; its `verified` flag remains false in the returned provenance
        so the model/UI can distinguish user material from curated knowledge.
        """
        limit = max(1, min(limit, 20))
        terms = self._fts_terms(query)
        requested_concepts = list(dict.fromkeys(concept_ids or []))
        retrieval_concepts = self._expand_concepts(subject_id, requested_concepts, depth=1)
        concept_clause, concept_params = self._concept_clause("k", retrieval_concepts)

        owner_clause = " AND k.owner_user_id IS NULL"
        owner_params: list[str] = []
        course_clause = ""
        course_params: list[str] = []
        course_access = False
        if user_id and course_id:
            course_access = bool(self.db.fetchone(
                """SELECT 1 FROM courses c LEFT JOIN course_enrollments e
                   ON e.course_id=c.course_id AND e.learner_user_id=? AND e.status='active'
                   WHERE c.course_id=? AND (c.owner_user_id=? OR e.learner_user_id=?)""",
                (user_id, course_id, user_id, user_id),
            ))
            if not course_access:
                return []
            # Enrolled learners may retrieve the teacher-owned material for that
            # course, but no private material from any other course.
            owner_clause = " AND (k.owner_user_id IS NULL OR k.course_id=?)"
            owner_params.append(course_id)
            course_clause = " AND (k.owner_user_id IS NULL OR k.course_id=?)"
            course_params.append(course_id)
        elif user_id:
            owner_clause = " AND (k.owner_user_id IS NULL OR k.owner_user_id=?)"
            owner_params.append(user_id)

        trust_clause = ""
        trust_params: list[object] = []
        if verified_only:
            if user_id and course_id and course_access:
                trust_clause = " AND (k.verified=1 OR k.course_id=?)"
                trust_params.append(course_id)
            elif user_id:
                trust_clause = " AND (k.verified=1 OR k.owner_user_id=?)"
                trust_params.append(user_id)
            else:
                trust_clause = " AND k.verified=1"

        candidates: list = []
        candidate_limit = max(limit * 8, 30)

        if terms and self.db.fts_available:
            sql = f"""
                SELECT k.*, bm25(knowledge_fts) AS fts_score
                FROM knowledge_fts
                JOIN knowledge_chunks k ON k.chunk_id=knowledge_fts.chunk_id
                WHERE knowledge_fts MATCH ?
                  AND k.subject_id=?
                  {owner_clause}
                  {course_clause}
                  {trust_clause}
                  {concept_clause}
                ORDER BY fts_score ASC
                LIMIT ?
            """
            try:
                candidates = self.db.fetchall(
                    sql,
                    (
                        terms,
                        subject_id,
                        *owner_params,
                        *course_params,
                        *trust_params,
                        *concept_params,
                        candidate_limit,
                    ),
                )
            except sqlite3.OperationalError:
                self.db.fts_available = False
                candidates = []

        if not candidates:
            sql = f"""
                SELECT k.*, 0.0 AS fts_score
                FROM knowledge_chunks k
                WHERE k.subject_id=?
                  {owner_clause}
                  {course_clause}
                  {trust_clause}
                  {concept_clause}
                ORDER BY k.created_at DESC
                LIMIT ?
            """
            candidates = self.db.fetchall(
                sql,
                (
                    subject_id,
                    *owner_params,
                    *course_params,
                    *trust_params,
                    *concept_params,
                    max(limit * 20, 100),
                ),
            )
            q_tokens = set(self._tokens(query))
            if q_tokens:
                candidates.sort(
                    key=lambda r: (
                        len(q_tokens & set(self._tokens(r["content"]))),
                        len(q_tokens & set(self._tokens(r["topic"] or ""))),
                    ),
                    reverse=True,
                )

        ranked: list[tuple[float, Any, list[str]]] = []
        requested = set(requested_concepts)
        for rank, row in enumerate(candidates):
            concepts = self.db.loads(row["concept_ids_json"], [])
            concepts = concepts if isinstance(concepts, list) else []
            base = 1.0 / (rank + 1.0)
            exact_boost = 0.35 if requested and requested.intersection(concepts) else 0.0
            verified_boost = 0.05 if bool(row["verified"]) else 0.0
            ranked.append((base + exact_boost + verified_boost, row, concepts))
        ranked.sort(key=lambda item: item[0], reverse=True)

        hits: list[KnowledgeHit] = []
        for score, row, concepts in ranked[:limit]:
            hits.append(
                KnowledgeHit(
                    chunk_id=row["chunk_id"],
                    source_id=row["source_id"],
                    subject_id=row["subject_id"],
                    content=row["content"],
                    concept_ids=concepts,
                    citation_location=row["citation_location"],
                    rank_score=score,
                    verified=bool(row["verified"]),
                )
            )
        return hits

    def get_chunks(
        self,
        chunk_ids: list[str],
        *,
        user_id: str | None = None,
        course_id: str | None = None,
    ) -> list[KnowledgeHit]:
        """Fetch known chunks without bypassing ownership/trust boundaries.

        Anonymous/internal callers receive only globally verified chunks. A user
        may additionally receive their own private chunks, optionally restricted
        to one course. This prevents a future direct-ID lookup from becoming an
        IDOR even if the method is later exposed by another subsystem.
        """
        if not chunk_ids:
            return []
        ids = list(dict.fromkeys(chunk_ids))[:100]
        placeholders = ",".join("?" for _ in ids)
        if user_id:
            if course_id:
                access = self.db.fetchone(
                    """SELECT 1 FROM courses c LEFT JOIN course_enrollments e ON e.course_id=c.course_id
                       AND e.learner_user_id=? AND e.status='active' WHERE c.course_id=?
                       AND (c.owner_user_id=? OR e.learner_user_id=?)""",
                    (user_id, course_id, user_id, user_id),
                )
                if not access:
                    return []
                scope = " AND (owner_user_id IS NULL OR course_id=?) AND (verified=1 OR course_id=?)"
                params: list[object] = [*ids, course_id, course_id]
            else:
                scope = " AND (owner_user_id IS NULL OR owner_user_id=?) AND (verified=1 OR owner_user_id=?)"
                params = [*ids, user_id, user_id]
        else:
            scope = " AND owner_user_id IS NULL AND verified=1"
            params = [*ids]
        rows = self.db.fetchall(
            f"SELECT * FROM knowledge_chunks WHERE chunk_id IN ({placeholders}){scope}",
            tuple(params),
        )
        by_id = {r["chunk_id"]: r for r in rows}
        result: list[KnowledgeHit] = []
        for rank, cid in enumerate(ids):
            row = by_id.get(cid)
            if not row:
                continue
            concepts = self.db.loads(row["concept_ids_json"], [])
            result.append(
                KnowledgeHit(
                    chunk_id=cid, source_id=row["source_id"], subject_id=row["subject_id"],
                    content=row["content"], concept_ids=concepts if isinstance(concepts, list) else [],
                    citation_location=row["citation_location"], rank_score=1.0 / (rank + 1.0),
                    verified=bool(row["verified"]),
                )
            )
        return result

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return re.findall(r"[\w]+", text.lower(), flags=re.UNICODE)

    @classmethod
    def _fts_terms(cls, query: str) -> str:
        tokens = cls._tokens(query)[:20]
        return " OR ".join(f'"{token}"' for token in tokens)
