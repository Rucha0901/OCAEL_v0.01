from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from threading import RLock
from uuid import uuid4

from .database import Database
from .schemas import (
    AdaptiveState,
    ConceptState,
    EvidenceEvent,
    GapHypothesis,
    LearningMap,
    MemoryMapEdge,
    MemoryMapNode,
    SessionRecord,
)


def _iso(dt: datetime | None = None) -> str:
    value = dt or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class MemoryService:
    """Owns long-term learner persistence and visible-map projection.

    The service is the single persistence writer used by the agent layer. The
    actual learner-state math is deliberately kept outside this module.
    """

    def __init__(self, db: Database, compaction_session_threshold: int = 40):
        self.db = db
        self.compaction_session_threshold = max(10, compaction_session_threshold)
        # Serialize read-modify-write learner updates inside this process. SQLite
        # transactions protect writes, but the learner math is computed before
        # persistence and otherwise could lose concurrent updates.
        self.write_lock = RLock()

    # ---------- Sessions ----------
    def start_session(
        self,
        *,
        user_id: str,
        subject_id: str,
        goal: str | None,
        source: str,
    ) -> SessionRecord:
        subject = self.db.fetchone("SELECT 1 FROM subjects WHERE subject_id=?", (subject_id,))
        if not subject:
            raise ValueError(f"Unknown subject_id: {subject_id}")
        session_id = str(uuid4())
        started = _iso()
        self.db.execute(
            """
            INSERT INTO sessions(session_id,user_id,subject_id,started_at,status,goal,source)
            VALUES(?,?,?,?, 'active', ?, ?)
            """,
            (session_id, user_id, subject_id, started, goal, source),
        )
        self.ensure_privacy_row(user_id)
        return SessionRecord(
            session_id=session_id,
            user_id=user_id,
            subject_id=subject_id,
            started_at=datetime.fromisoformat(started),
            goal=goal,
        )

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self.db.fetchone("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if not row:
            return None
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            subject_id=row["subject_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            status=row["status"],
            goal=row["goal"],
        )

    def complete_session(self, session_id: str, status: str = "completed") -> dict[str, Any]:
        if status not in {"completed", "abandoned"}:
            raise ValueError("status must be completed or abandoned")
        with self.write_lock:
            session = self.get_session(session_id)
            if not session:
                raise ValueError("Session not found")
            if session.status != "active":
                # Completion is idempotent for an already-closed session: preserve
                # the original end time/status rather than rewriting history.
                return self.build_session_summary(session_id)
            ended = _iso()
            self.db.execute(
                "UPDATE sessions SET ended_at=?, status=? WHERE session_id=?",
                (ended, status, session_id),
            )
            summary = self.build_session_summary(session_id)
            self._maybe_compact(session.user_id, session.subject_id)
            return summary

    # ---------- Single-writer commit ----------
    def commit_learning_update(
        self, *, event: EvidenceEvent, states: list[ConceptState],
        hypotheses: list[GapHypothesis] | None = None,
        target_concept_id: str | None = None,
        adaptive_state: AdaptiveState | None = None,
    ) -> None:
        with self.write_lock:
            session = self.get_session(event.session_id)
            if not session:
                raise ValueError("Cannot record an event for an unknown session")
            if session.status != "active":
                raise ValueError("Cannot record an event into a completed session")
            if session.user_id != event.user_id or session.subject_id != event.subject_id:
                raise ValueError("Event identity does not match the session")
            if not self.personalization_enabled(event.user_id):
                return

            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO learning_events(
                        event_id,session_id,user_id,subject_id,primary_concept_id,concept_ids_json,
                        timestamp,activity_type,input_mode,correct,score,evidence_strength,response_text,
                        mistake_type,concept_likelihoods_json,metadata_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event.event_id,event.session_id,event.user_id,event.subject_id,event.primary_concept_id,
                        self.db.dumps(event.concept_ids),_iso(event.timestamp),event.activity_type,event.input_mode,
                        None if event.correct is None else int(event.correct),event.score,event.evidence_strength,
                        event.response_text,event.mistake_type,self.db.dumps(event.concept_likelihoods),
                        self.db.dumps(event.metadata),
                    ),
                )
                now = _iso()
                for state in states:
                    if state.user_id != event.user_id:
                        raise ValueError("Concept state user identity does not match event")
                    conn.execute(
                        """
                        INSERT INTO concept_state(
                            user_id,concept_id,mastery_belief,confidence,evidence_count,last_observed_at,
                            status,recurrence_count,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(user_id,concept_id) DO UPDATE SET
                            mastery_belief=excluded.mastery_belief,confidence=excluded.confidence,
                            evidence_count=excluded.evidence_count,last_observed_at=excluded.last_observed_at,
                            status=excluded.status,recurrence_count=excluded.recurrence_count,updated_at=excluded.updated_at
                        """,
                        (state.user_id,state.concept_id,state.mastery_belief,state.confidence,state.evidence_count,
                         _iso(state.last_observed_at) if state.last_observed_at else None,state.status,
                         state.recurrence_count,now),
                    )

                if hypotheses is not None and target_concept_id:
                    conn.execute(
                        "DELETE FROM gap_hypotheses WHERE user_id=? AND target_concept_id=?",
                        (event.user_id,target_concept_id),
                    )
                    for hypothesis in hypotheses:
                        conn.execute(
                            """INSERT INTO gap_hypotheses(
                              user_id,target_concept_id,gap_concept_id,probability,source,evidence_json,updated_at
                            ) VALUES(?,?,?,?,?,?,?)""",
                            (event.user_id,target_concept_id,hypothesis.concept_id,hypothesis.probability,
                             hypothesis.source,self.db.dumps(hypothesis.evidence),now),
                        )

                if adaptive_state is not None:
                    if (adaptive_state.user_id != event.user_id or adaptive_state.subject_id != event.subject_id
                            or adaptive_state.concept_id != event.primary_concept_id):
                        raise ValueError("Adaptive state identity does not match event")
                    conn.execute(
                        """
                        INSERT INTO adaptive_state(
                          user_id,subject_id,concept_id,theta,variance,target_difficulty,difficulty_band,
                          observations,micro_stage_position,behavior_profile,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(user_id,subject_id,concept_id) DO UPDATE SET
                          theta=excluded.theta,variance=excluded.variance,target_difficulty=excluded.target_difficulty,
                          difficulty_band=excluded.difficulty_band,observations=excluded.observations,
                          micro_stage_position=excluded.micro_stage_position,behavior_profile=excluded.behavior_profile,
                          updated_at=excluded.updated_at
                        """,
                        (adaptive_state.user_id,adaptive_state.subject_id,adaptive_state.concept_id,
                         adaptive_state.theta,adaptive_state.variance,adaptive_state.target_difficulty,
                         adaptive_state.difficulty_band,adaptive_state.observations,adaptive_state.micro_stage_position,
                         adaptive_state.behavior_profile,now),
                    )

    # ---------- Read paths ----------
    def recent_events(
        self,
        *,
        user_id: str,
        subject_id: str | None = None,
        concept_id: str | None = None,
        limit: int = 30,
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        params: list[Any] = [user_id]
        if subject_id:
            clauses.append("subject_id=?")
            params.append(subject_id)
        if concept_id:
            clauses.append(
                "(primary_concept_id=? OR concept_ids_json LIKE ?)"
            )
            params.extend([concept_id, f'%"{concept_id}"%'])
        if before:
            clauses.append("timestamp<?")
            params.append(_iso(before))
        params.append(max(1, min(limit, 200)))
        rows = self.db.fetchall(
            f"""
            SELECT * FROM learning_events
            WHERE {' AND '.join(clauses)}
            ORDER BY timestamp DESC LIMIT ?
            """,
            tuple(params),
        )
        return [self._event_row(row) for row in rows]

    def get_concept_state(self, user_id: str, concept_id: str) -> ConceptState | None:
        row = self.db.fetchone(
            "SELECT * FROM concept_state WHERE user_id=? AND concept_id=?",
            (user_id, concept_id),
        )
        if not row:
            return None
        return ConceptState(
            user_id=row["user_id"],
            concept_id=row["concept_id"],
            mastery_belief=row["mastery_belief"],
            confidence=row["confidence"],
            evidence_count=row["evidence_count"],
            last_observed_at=datetime.fromisoformat(row["last_observed_at"])
            if row["last_observed_at"]
            else None,
            status=row["status"],
            recurrence_count=row["recurrence_count"],
        )

    def list_concept_states(self, user_id: str, subject_id: str | None = None) -> list[ConceptState]:
        if subject_id:
            rows = self.db.fetchall(
                """
                SELECT s.* FROM concept_state s
                JOIN concepts c ON c.concept_id=s.concept_id
                WHERE s.user_id=? AND c.subject_id=?
                ORDER BY c.name
                """,
                (user_id, subject_id),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM concept_state WHERE user_id=? ORDER BY concept_id",
                (user_id,),
            )
        result: list[ConceptState] = []
        for row in rows:
            result.append(
                ConceptState(
                    user_id=row["user_id"],
                    concept_id=row["concept_id"],
                    mastery_belief=row["mastery_belief"],
                    confidence=row["confidence"],
                    evidence_count=row["evidence_count"],
                    last_observed_at=datetime.fromisoformat(row["last_observed_at"])
                    if row["last_observed_at"]
                    else None,
                    status=row["status"],
                    recurrence_count=row["recurrence_count"],
                )
            )
        return result

    def get_gap_hypotheses(self, user_id: str, target_concept_id: str) -> list[GapHypothesis]:
        rows = self.db.fetchall(
            """
            SELECT * FROM gap_hypotheses
            WHERE user_id=? AND target_concept_id=?
            ORDER BY probability DESC
            """,
            (user_id, target_concept_id),
        )
        return [
            GapHypothesis(
                concept_id=r["gap_concept_id"],
                probability=r["probability"],
                source=r["source"],
                evidence=self.db.loads(r["evidence_json"], []),
            )
            for r in rows
        ]

    def build_context_capsule(
        self,
        *,
        user_id: str,
        subject_id: str,
        target_concept_id: str,
        recent_limit: int = 5,
    ) -> dict[str, Any]:
        # Pausing personalization means historical learner memory must not be
        # consulted, not merely that new writes stop.
        if not self.personalization_enabled(user_id):
            return {
                "subject": subject_id,
                "target_concept": target_concept_id,
                "states": {},
                "relevant_history": [],
                "historical_context": [],
                "gap_hypotheses": [],
            }

        states: dict[str, Any] = {}
        target = self.get_concept_state(user_id, target_concept_id)
        if target:
            states[target_concept_id] = {
                "mastery": round(target.mastery_belief, 4),
                "confidence": round(target.confidence, 4),
                "status": target.status,
            }

        edge_rows = self.db.fetchall(
            """
            SELECT source_concept_id FROM concept_edges
            WHERE target_concept_id=? AND relation='PREREQUISITE_OF'
            ORDER BY strength DESC LIMIT 4
            """,
            (target_concept_id,),
        )
        for edge in edge_rows:
            cid = edge["source_concept_id"]
            state = self.get_concept_state(user_id, cid)
            if state:
                states[cid] = {
                    "mastery": round(state.mastery_belief, 4),
                    "confidence": round(state.confidence, 4),
                    "status": state.status,
                }

        events = self.recent_events(
            user_id=user_id, subject_id=subject_id,
            concept_id=target_concept_id, limit=recent_limit,
        )
        history = [
            {
                "activity": e["activity_type"],
                "correct": e["correct"],
                "mistake_type": e["mistake_type"],
                "timestamp": e["timestamp"],
            }
            for e in events
        ]
        return {
            "subject": subject_id,
            "target_concept": target_concept_id,
            "states": states,
            "relevant_history": history,
            "historical_context": self.relevant_compacted_history(
                user_id=user_id, subject_id=subject_id,
                concept_id=target_concept_id, limit=2,
            ),
            "gap_hypotheses": [
                h.model_dump() for h in self.get_gap_hypotheses(user_id, target_concept_id)[:4]
            ],
        }

    def recent_question_ids(self, user_id: str, subject_id: str, limit: int = 30) -> set[str]:
        """Questions recently answered *or presented* to avoid repeated prompts."""
        limit = max(1, min(limit, 100))
        rows = self.db.fetchall(
            """
            SELECT metadata_json, timestamp AS ts FROM learning_events
            WHERE user_id=? AND subject_id=?
            UNION ALL
            SELECT metadata_json, created_at AS ts FROM interventions
            WHERE user_id=? AND subject_id=?
            ORDER BY ts DESC LIMIT ?
            """,
            (user_id, subject_id, user_id, subject_id, limit),
        )
        result: set[str] = set()
        for row in rows:
            metadata = self.db.loads(row["metadata_json"], {})
            if not isinstance(metadata, dict):
                continue
            qid = metadata.get("question_id") or metadata.get("selected_question_id")
            if isinstance(qid, str) and qid:
                result.add(qid)
        return result

    def relevant_compacted_history(
        self, *, user_id: str, subject_id: str, concept_id: str, limit: int = 2
    ) -> list[dict[str, Any]]:
        if not self.personalization_enabled(user_id):
            return []
        rows = self.db.fetchall(
            """
            SELECT start_at,end_at,summary_json FROM compacted_history
            WHERE user_id=? AND subject_id=?
            ORDER BY end_at DESC LIMIT 20
            """,
            (user_id, subject_id),
        )
        matched: list[dict[str, Any]] = []
        fallback: list[dict[str, Any]] = []
        for row in rows:
            summary = self.db.loads(row["summary_json"], {})
            if not isinstance(summary, dict):
                continue
            item = {"start_at": row["start_at"], "end_at": row["end_at"], "summary": summary}
            fallback.append(item)
            concepts = summary.get("top_concepts", [])
            if isinstance(concepts, list) and concept_id in concepts:
                matched.append(item)
        return (matched or fallback)[: max(0, min(limit, 5))]

    # ---------- Summaries / compaction ----------
    def build_session_summary(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        if not session:
            raise ValueError("Session not found")
        rows = self.db.fetchall(
            "SELECT * FROM learning_events WHERE session_id=? ORDER BY timestamp",
            (session_id,),
        )
        events = [self._event_row(row) for row in rows]
        intervention_rows = self.db.fetchall(
            "SELECT concept_id,action_type FROM interventions WHERE session_id=? ORDER BY created_at",
            (session_id,),
        )
        concepts = Counter()
        mistakes = Counter()
        intervention_actions = Counter()
        correct = 0
        answered = 0
        for event in events:
            concepts.update(event["concept_ids"])
            if event["mistake_type"]:
                mistakes[event["mistake_type"]] += 1
            if event["correct"] is not None:
                answered += 1
                correct += int(bool(event["correct"]))
        for row in intervention_rows:
            concepts.update([row["concept_id"]])
            intervention_actions[row["action_type"]] += 1

        summary = {
            "session_id": session_id,
            "subject_id": session.subject_id,
            "goal": session.goal,
            "event_count": len(events),
            "intervention_count": len(intervention_rows),
            "intervention_actions": dict(intervention_actions.most_common()),
            "concepts": [c for c, _ in concepts.most_common()],
            "mistakes": dict(mistakes.most_common()),
            "answered": answered,
            "correct": correct,
            "accuracy": (correct / answered) if answered else None,
            "started_at": session.started_at.isoformat(),
            "ended_at": session.ended_at.isoformat() if session.ended_at else _iso(),
        }
        self.db.execute(
            """
            INSERT INTO session_summaries(session_id,user_id,subject_id,summary_json,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
              summary_json=excluded.summary_json,
              created_at=excluded.created_at
            """,
            (
                session_id,
                session.user_id,
                session.subject_id,
                self.db.dumps(summary),
                _iso(),
            ),
        )
        return summary

    def _maybe_compact(self, user_id: str, subject_id: str) -> None:
        latest = self.db.fetchone(
            "SELECT end_at FROM compacted_history WHERE user_id=? AND subject_id=? ORDER BY end_at DESC LIMIT 1",
            (user_id, subject_id),
        )
        after = latest["end_at"] if latest else ""
        count_row = self.db.fetchone(
            """
            SELECT count(*) AS n FROM session_summaries ss
            JOIN sessions s USING(session_id)
            WHERE ss.user_id=? AND ss.subject_id=? AND s.started_at>?
            """,
            (user_id, subject_id, after),
        )
        if not count_row or count_row["n"] < self.compaction_session_threshold:
            return

        batch = max(5, self.compaction_session_threshold // 2)
        rows = self.db.fetchall(
            """
            SELECT ss.*, s.started_at, s.ended_at
            FROM session_summaries ss JOIN sessions s USING(session_id)
            WHERE ss.user_id=? AND ss.subject_id=? AND s.started_at>?
            ORDER BY s.started_at ASC LIMIT ?
            """,
            (user_id, subject_id, after, batch),
        )
        if len(rows) < 5:
            return

        concept_counts: Counter[str] = Counter()
        mistake_counts: Counter[str] = Counter()
        intervention_counts: Counter[str] = Counter()
        answered = correct = 0
        source_ids = []
        for row in rows:
            source_ids.append(row["session_id"])
            summary = self.db.loads(row["summary_json"], {})
            if not isinstance(summary, dict):
                continue
            concepts = summary.get("concepts", [])
            mistakes = summary.get("mistakes", {})
            interventions = summary.get("intervention_actions", {})
            if isinstance(concepts, list): concept_counts.update(str(c) for c in concepts)
            if isinstance(mistakes, dict): mistake_counts.update({str(k): int(v) for k, v in mistakes.items() if isinstance(v, (int,float))})
            if isinstance(interventions, dict): intervention_counts.update({str(k): int(v) for k, v in interventions.items() if isinstance(v, (int,float))})
            answered += int(summary.get("answered") or 0)
            correct += int(summary.get("correct") or 0)

        compact = {
            "sessions": len(rows),
            "top_concepts": [x for x, _ in concept_counts.most_common(12)],
            "mistakes": dict(mistake_counts.most_common(12)),
            "intervention_actions": dict(intervention_counts.most_common(12)),
            "answered": answered, "correct": correct,
            "accuracy": correct / answered if answered else None,
        }
        self.db.execute(
            """
            INSERT INTO compacted_history(
              compact_id,user_id,subject_id,start_at,end_at,source_session_ids_json,summary_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (str(uuid4()), user_id, subject_id, rows[0]["started_at"],
             rows[-1]["ended_at"] or rows[-1]["started_at"],
             self.db.dumps(source_ids), self.db.dumps(compact), _iso()),
        )

    # ---------- Visible map ----------
    def learning_map(self, user_id: str, subject_id: str | None = None) -> LearningMap:
        if subject_id:
            concept_rows = self.db.fetchall(
                "SELECT * FROM concepts WHERE subject_id=? ORDER BY name", (subject_id,)
            )
        else:
            concept_rows = self.db.fetchall("SELECT * FROM concepts ORDER BY subject_id,name")

        state_rows = {
            r["concept_id"]: r
            for r in self.db.fetchall("SELECT * FROM concept_state WHERE user_id=?", (user_id,))
        }
        nodes = [
            MemoryMapNode(
                id=r["concept_id"], label=r["name"], kind="concept",
                subject_id=r["subject_id"],
                status=state_rows[r["concept_id"]]["status"] if r["concept_id"] in state_rows else "unknown",
            )
            for r in concept_rows
        ]
        if subject_id:
            rows = self.db.fetchall(
                """
                SELECT e.* FROM concept_edges e
                JOIN concepts s ON s.concept_id=e.source_concept_id
                JOIN concepts t ON t.concept_id=e.target_concept_id
                WHERE s.subject_id=? AND t.subject_id=?
                """,
                (subject_id, subject_id),
            )
        else:
            rows = self.db.fetchall("SELECT * FROM concept_edges")
        edges = [
            MemoryMapEdge(source=r["source_concept_id"], target=r["target_concept_id"], relation=r["relation"])
            for r in rows
        ]
        return LearningMap(nodes=nodes, edges=edges)

    # ---------- Privacy ----------
    def ensure_privacy_row(self, user_id: str) -> None:
        self.db.execute(
            """
            INSERT INTO privacy_settings(user_id,personalization_enabled,updated_at)
            VALUES(?,1,?) ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, _iso()),
        )

    def personalization_enabled(self, user_id: str) -> bool:
        self.ensure_privacy_row(user_id)
        row = self.db.fetchone(
            "SELECT personalization_enabled FROM privacy_settings WHERE user_id=?",
            (user_id,),
        )
        return bool(row and row["personalization_enabled"])

    def set_personalization(self, user_id: str, enabled: bool) -> None:
        with self.write_lock:
            self.db.execute(
                """
                INSERT INTO privacy_settings(user_id,personalization_enabled,updated_at)
                VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                  personalization_enabled=excluded.personalization_enabled,
                  updated_at=excluded.updated_at
                """,
                (user_id, int(enabled), _iso()),
            )

    def delete_memory(
        self, *, user_id: str, subject_id: str | None = None,
        start_at: datetime | None = None, end_at: datetime | None = None,
        delete_all: bool = False,
    ) -> set[str]:
        if not delete_all and not subject_id and not start_at and not end_at:
            raise ValueError("Specify a subject/date range or delete_all=true")
        full_subject_clear = bool(subject_id and start_at is None and end_at is None)

        # A full subject reset must invalidate state even when no retained event
        # currently references a concept.
        affected: set[str] = set()
        affected_subjects: set[str] = set()
        if subject_id and start_at is None and end_at is None:
            affected.update(
                r["concept_id"] for r in self.db.fetchall(
                    "SELECT concept_id FROM concepts WHERE subject_id=?", (subject_id,)
                )
            )
            affected_subjects.add(subject_id)

        clauses = ["user_id=?"]
        params: list[Any] = [user_id]
        if subject_id:
            clauses.append("subject_id=?"); params.append(subject_id)
        if start_at:
            clauses.append("timestamp>=?"); params.append(_iso(start_at))
        if end_at:
            clauses.append("timestamp<=?"); params.append(_iso(end_at))
        event_rows = self.db.fetchall(
            f"SELECT session_id,subject_id,concept_ids_json FROM learning_events WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        affected_sessions: set[str] = set()
        for row in event_rows:
            vals = self.db.loads(row["concept_ids_json"], [])
            if isinstance(vals, list): affected.update(str(v) for v in vals)
            affected_sessions.add(row["session_id"])
            affected_subjects.add(row["subject_id"])
        if full_subject_clear:
            affected_sessions.update(
                row["session_id"] for row in self.db.fetchall(
                    "SELECT session_id FROM sessions WHERE user_id=? AND subject_id=?",
                    (user_id, subject_id),
                )
            )

        with self.write_lock, self.db.transaction() as conn:
            if delete_all:
                # Sessions cascade events, interventions, and summaries. Preserve
                # the user's privacy preference instead of silently re-enabling it.
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM concept_state WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM adaptive_state WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM gap_hypotheses WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM compacted_history WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM causal_gap_cases WHERE user_id=?", (user_id,))
                return affected

            conn.execute(f"DELETE FROM learning_events WHERE {' AND '.join(clauses)}", tuple(params))

            int_clauses = ["user_id=?"]
            int_params: list[Any] = [user_id]
            if subject_id:
                int_clauses.append("subject_id=?"); int_params.append(subject_id)
            if start_at:
                int_clauses.append("created_at>=?"); int_params.append(_iso(start_at))
            if end_at:
                int_clauses.append("created_at<=?"); int_params.append(_iso(end_at))
            int_rows = conn.execute(
                f"SELECT session_id,subject_id,concept_id FROM interventions WHERE {' AND '.join(int_clauses)}",
                tuple(int_params),
            ).fetchall()
            for row in int_rows:
                affected.add(row["concept_id"]); affected_sessions.add(row["session_id"])
                affected_subjects.add(row["subject_id"])
            conn.execute(f"DELETE FROM interventions WHERE {' AND '.join(int_clauses)}", tuple(int_params))

            if full_subject_clear:
                conn.execute(
                    "DELETE FROM sessions WHERE user_id=? AND subject_id=?",
                    (user_id, subject_id),
                )
            else:
                for session_id in affected_sessions:
                    conn.execute("DELETE FROM session_summaries WHERE session_id=?", (session_id,))

            # Invalidate only compactions whose represented period overlaps the
            # deleted range; a full subject reset removes all subject compactions.
            comp_clauses = ["user_id=?"]
            comp_params: list[Any] = [user_id]
            if subject_id:
                comp_clauses.append("subject_id=?"); comp_params.append(subject_id)
            if start_at:
                comp_clauses.append("end_at>=?"); comp_params.append(_iso(start_at))
            if end_at:
                comp_clauses.append("start_at<=?"); comp_params.append(_iso(end_at))
            conn.execute(
                f"DELETE FROM compacted_history WHERE {' AND '.join(comp_clauses)}", tuple(comp_params)
            )

            for concept_id in affected:
                conn.execute(
                    "DELETE FROM causal_gap_cases WHERE user_id=? AND target_concept_id=?",
                    (user_id, concept_id),
                )
                conn.execute("DELETE FROM concept_state WHERE user_id=? AND concept_id=?", (user_id, concept_id))
                conn.execute("DELETE FROM adaptive_state WHERE user_id=? AND concept_id=?", (user_id, concept_id))
                conn.execute(
                    "DELETE FROM gap_hypotheses WHERE user_id=? AND (target_concept_id=? OR gap_concept_id=?)",
                    (user_id, concept_id, concept_id),
                )

            # Only remove sessions touched by this deletion and now truly empty;
            # never sweep unrelated empty/active sessions.
            if not full_subject_clear:
                for session_id in affected_sessions:
                    remaining_e = conn.execute("SELECT 1 FROM learning_events WHERE session_id=? LIMIT 1", (session_id,)).fetchone()
                    remaining_i = conn.execute("SELECT 1 FROM interventions WHERE session_id=? LIMIT 1", (session_id,)).fetchone()
                    row = conn.execute("SELECT status FROM sessions WHERE session_id=?", (session_id,)).fetchone()
                    if row and not remaining_e and not remaining_i and row["status"] != "active":
                        conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

        if full_subject_clear:
            return affected
        for session_id in affected_sessions:
            session = self.get_session(session_id)
            if session and session.status != "active":
                remaining_event = self.db.fetchone(
                    "SELECT 1 FROM learning_events WHERE session_id=? LIMIT 1", (session_id,)
                )
                remaining_intervention = self.db.fetchone(
                    "SELECT 1 FROM interventions WHERE session_id=? LIMIT 1", (session_id,)
                )
                if remaining_event or remaining_intervention:
                    self.build_session_summary(session_id)

        # Any deleted historical slice can invalidate the roll-up that contained
        # it. Rebuild compactions from retained session summaries rather than
        # leaving a hole or a stale aggregate that still reflects deleted data.
        for affected_subject in sorted(affected_subjects):
            self._rebuild_compacted_history(user_id, affected_subject)
        return affected

    def _rebuild_compacted_history(self, user_id: str, subject_id: str) -> None:
        self.db.execute(
            "DELETE FROM compacted_history WHERE user_id=? AND subject_id=?",
            (user_id, subject_id),
        )
        while True:
            before = self.db.fetchone(
                "SELECT count(*) AS n FROM compacted_history WHERE user_id=? AND subject_id=?",
                (user_id, subject_id),
            )["n"]
            self._maybe_compact(user_id, subject_id)
            after = self.db.fetchone(
                "SELECT count(*) AS n FROM compacted_history WHERE user_id=? AND subject_id=?",
                (user_id, subject_id),
            )["n"]
            if after == before:
                break

    @staticmethod
    def _event_row(row: Any) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "subject_id": row["subject_id"],
            "primary_concept_id": row["primary_concept_id"],
            "concept_ids": Database.loads(row["concept_ids_json"], []),
            "timestamp": row["timestamp"],
            "activity_type": row["activity_type"],
            "input_mode": row["input_mode"],
            "correct": None if row["correct"] is None else bool(row["correct"]),
            "score": row["score"],
            "evidence_strength": row["evidence_strength"],
            "response_text": row["response_text"],
            "mistake_type": row["mistake_type"],
            "concept_likelihoods": Database.loads(row["concept_likelihoods_json"], {}),
            "metadata": Database.loads(row["metadata_json"], {}),
        }

    def record_intervention(
        self, *, session_id: str, user_id: str, subject_id: str, concept_id: str,
        action_type: str, content: str, grounded: bool, source_ids: list[str],
        supervisor_used: bool, metadata: dict[str, Any] | None = None,
    ) -> str:
        with self.write_lock:
            session = self.get_session(session_id)
            if (not session or session.user_id != user_id or session.subject_id != subject_id
                    or session.status != "active"):
                raise ValueError("Intervention does not match an active learner session")
            concept = self.db.fetchone("SELECT subject_id FROM concepts WHERE concept_id=?", (concept_id,))
            if not concept or concept["subject_id"] != subject_id:
                raise ValueError("Intervention concept does not belong to the active subject")
            if not self.personalization_enabled(user_id):
                return ""
            intervention_id = str(uuid4())
            self.db.execute(
                """
                INSERT INTO interventions(
                  intervention_id,session_id,user_id,subject_id,concept_id,action_type,content,grounded,
                  source_ids_json,supervisor_used,created_at,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (intervention_id,session_id,user_id,subject_id,concept_id,action_type,content,
                 int(grounded),self.db.dumps(source_ids),int(supervisor_used),_iso(),self.db.dumps(metadata or {})),
            )
            return intervention_id
