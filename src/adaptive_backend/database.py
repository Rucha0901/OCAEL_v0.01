from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterator


CORE_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS subjects (
    subject_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS concepts (
    concept_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    bkt_params_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_concepts_subject ON concepts(subject_id);

CREATE TABLE IF NOT EXISTS concept_edges (
    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    target_concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 1.0 CHECK(strength >= 0.0 AND strength <= 1.0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_concept_id, target_concept_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_edges_target ON concept_edges(target_concept_id, relation);
CREATE INDEX IF NOT EXISTS idx_edges_source ON concept_edges(source_concept_id, relation);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    goal TEXT,
    source TEXT NOT NULL DEFAULT 'text'
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_subject_time
ON sessions(user_id, subject_id, started_at DESC);

CREATE TABLE IF NOT EXISTS learning_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    primary_concept_id TEXT NOT NULL,
    concept_ids_json TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    input_mode TEXT NOT NULL,
    correct INTEGER,
    score REAL,
    evidence_strength REAL NOT NULL DEFAULT 1.0,
    response_text TEXT,
    mistake_type TEXT,
    concept_likelihoods_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_user_subject_time
ON learning_events(user_id, subject_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_user_concept_time
ON learning_events(user_id, primary_concept_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_session ON learning_events(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_mistake ON learning_events(user_id, mistake_type, timestamp DESC);

CREATE TABLE IF NOT EXISTS concept_state (
    user_id TEXT NOT NULL,
    concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    mastery_belief REAL NOT NULL,
    confidence REAL NOT NULL,
    evidence_count INTEGER NOT NULL,
    last_observed_at TEXT,
    status TEXT NOT NULL,
    recurrence_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, concept_id)
);
CREATE INDEX IF NOT EXISTS idx_state_user_status ON concept_state(user_id, status);

CREATE TABLE IF NOT EXISTS adaptive_state (
    user_id TEXT NOT NULL,
    subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    theta REAL NOT NULL DEFAULT 0.0,
    variance REAL NOT NULL DEFAULT 2.25 CHECK(variance > 0.0),
    target_difficulty REAL NOT NULL DEFAULT 0.5 CHECK(target_difficulty >= 0.0 AND target_difficulty <= 1.0),
    difficulty_band TEXT NOT NULL DEFAULT 'medium',
    observations INTEGER NOT NULL DEFAULT 0,
    micro_stage_position INTEGER NOT NULL DEFAULT 0,
    behavior_profile TEXT NOT NULL DEFAULT 'insufficient_evidence',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, subject_id, concept_id)
);
CREATE INDEX IF NOT EXISTS idx_adaptive_user_subject
ON adaptive_state(user_id, subject_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS gap_hypotheses (
    user_id TEXT NOT NULL,
    target_concept_id TEXT NOT NULL,
    gap_concept_id TEXT NOT NULL,
    probability REAL NOT NULL,
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, target_concept_id, gap_concept_id)
);
CREATE INDEX IF NOT EXISTS idx_gap_target
ON gap_hypotheses(user_id, target_concept_id, probability DESC);

CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
    primary_concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    secondary_concept_ids_json TEXT NOT NULL DEFAULT '[]',
    stem TEXT NOT NULL,
    answer_type TEXT NOT NULL,
    answer_key_json TEXT NOT NULL,
    difficulty REAL NOT NULL DEFAULT 0.5,
    is_diagnostic INTEGER NOT NULL DEFAULT 0,
    diagnostic_model_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_questions_subject_concept
ON questions(subject_id, primary_concept_id, is_diagnostic);

CREATE TABLE IF NOT EXISTS interventions (
    intervention_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    content TEXT NOT NULL,
    grounded INTEGER NOT NULL DEFAULT 0,
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    supervisor_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_interventions_user_concept
ON interventions(user_id, concept_id, created_at DESC);

CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_user_subject
ON session_summaries(user_id, subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS compacted_history (
    compact_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    source_session_ids_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compact_user_subject_time
ON compacted_history(user_id, subject_id, end_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT PRIMARY KEY,
    owner_user_id TEXT,
    course_id TEXT,
    document_id TEXT,
    source_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    unit TEXT,
    topic TEXT,
    concept_ids_json TEXT NOT NULL DEFAULT '[]',
    prerequisite_ids_json TEXT NOT NULL DEFAULT '[]',
    difficulty REAL NOT NULL DEFAULT 0.5,
    content_type TEXT NOT NULL,
    content TEXT NOT NULL,
    citation_location TEXT,
    verified INTEGER NOT NULL DEFAULT 1,
    version TEXT NOT NULL DEFAULT '1',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_subject ON knowledge_chunks(subject_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_owner ON knowledge_chunks(owner_user_id,subject_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_course ON knowledge_chunks(course_id,subject_id);

CREATE TABLE IF NOT EXISTS privacy_settings (
    user_id TEXT PRIMARY KEY,
    personalization_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
"""


FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    chunk_id UNINDEXED,
    content,
    topic,
    unit,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge_chunks BEGIN
  INSERT INTO knowledge_fts(chunk_id, content, topic, unit)
  VALUES (new.chunk_id, new.content, coalesce(new.topic,''), coalesce(new.unit,''));
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge_chunks BEGIN
  DELETE FROM knowledge_fts WHERE chunk_id = old.chunk_id;
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge_chunks BEGIN
  DELETE FROM knowledge_fts WHERE chunk_id = old.chunk_id;
  INSERT INTO knowledge_fts(chunk_id, content, topic, unit)
  VALUES (new.chunk_id, new.content, coalesce(new.topic,''), coalesce(new.unit,''));
END;

"""


OVAEL_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    recovery_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    refresh_token_hash TEXT,
    refresh_expires_at TEXT,
    revoked_at TEXT,
    user_agent TEXT,
    ip_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at DESC);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    action TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    window_started_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    blocked_until TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(action, key_hash)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id TEXT PRIMARY KEY,
    user_id TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    client_type TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_log(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    subject_id TEXT,
    name TEXT NOT NULL,
    description TEXT,
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_courses_owner ON courses(owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    course_id TEXT,
    filename TEXT NOT NULL,
    content_type TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    storage_key TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'uploaded',
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, course_id, sha256, version)
);
CREATE INDEX IF NOT EXISTS idx_docs_owner ON documents(owner_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_docs_course ON documents(course_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS document_pages (
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    extracted_text TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(document_id, page_no)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    resource_id TEXT,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 3,
    progress REAL NOT NULL DEFAULT 0.0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    idempotency_key TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, kind, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_owner_time ON jobs(owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS teaching_sessions (
    teaching_session_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    course_id TEXT,
    concept_id TEXT NOT NULL,
    goal TEXT,
    mode TEXT NOT NULL,
    source_client TEXT NOT NULL DEFAULT 'web',
    status TEXT NOT NULL DEFAULT 'active',
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_teaching_owner ON teaching_sessions(owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS teaching_turns (
    turn_id TEXT PRIMARY KEY,
    teaching_session_id TEXT NOT NULL REFERENCES teaching_sessions(teaching_session_id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    learner_response_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    move_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(teaching_session_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS teaching_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    teaching_session_id TEXT NOT NULL REFERENCES teaching_sessions(teaching_session_id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL,
    suggestion TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_teaching_suggestions_session
ON teaching_suggestions(teaching_session_id, owner_user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS generated_items (
    generated_item_id TEXT PRIMARY KEY,
    owner_user_id TEXT,
    subject_id TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    generator_model TEXT,
    requested_difficulty REAL NOT NULL,
    difficulty_prior REAL NOT NULL,
    difficulty_posterior REAL NOT NULL,
    calibration_status TEXT NOT NULL DEFAULT 'provisional',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    independent_attempt_count INTEGER NOT NULL DEFAULT 0,
    correct_count REAL NOT NULL DEFAULT 0.0,
    hinted_count INTEGER NOT NULL DEFAULT 0,
    calibration_bucket_key TEXT,
    item_json TEXT NOT NULL,
    validation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generated_concept ON generated_items(subject_id, concept_id, created_at DESC);

CREATE TABLE IF NOT EXISTS item_calibration_buckets (
    bucket_key TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    answer_type TEXT NOT NULL,
    generator_model TEXT NOT NULL,
    requested_difficulty_center REAL NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    independent_attempt_count INTEGER NOT NULL DEFAULT 0,
    correct_sum REAL NOT NULL DEFAULT 0.0,
    hinted_or_retried_count INTEGER NOT NULL DEFAULT 0,
    difficulty_posterior REAL NOT NULL,
    calibration_status TEXT NOT NULL DEFAULT 'provisional',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calibration_subject_concept
ON item_calibration_buckets(subject_id, concept_id, answer_type, generator_model);

CREATE TABLE IF NOT EXISTS mcp_context_relay (
    user_id TEXT PRIMARY KEY,
    capsule_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_connections (
    connection_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    client_name TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_user ON mcp_connections(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS external_learning_inbox (
    inbox_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    external_session_id TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, external_session_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_external_inbox_user ON external_learning_inbox(user_id, status, created_at);

CREATE TABLE IF NOT EXISTS handoff_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    teaching_session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
"""


class Database:
    """Small SQLite persistence layer.

    The class intentionally keeps persistence in one module. Business rules stay in
    the learner-state, diagnosis, retrieval, and memory modules.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.fts_available = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(CORE_SCHEMA)
            conn.executescript(OVAEL_SCHEMA)
            # Forward-compatible local migration for databases created before OVAEL v0.01.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_chunks)").fetchall()}
            for name in ("owner_user_id", "course_id", "document_id"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE knowledge_chunks ADD COLUMN {name} TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_owner ON knowledge_chunks(owner_user_id,subject_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_course ON knowledge_chunks(course_id,subject_id)")
            auth_columns = {row[1] for row in conn.execute("PRAGMA table_info(auth_sessions)").fetchall()}
            if "refresh_token_hash" not in auth_columns:
                conn.execute("ALTER TABLE auth_sessions ADD COLUMN refresh_token_hash TEXT")
            if "refresh_expires_at" not in auth_columns:
                conn.execute("ALTER TABLE auth_sessions ADD COLUMN refresh_expires_at TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_refresh_hash ON auth_sessions(refresh_token_hash) WHERE refresh_token_hash IS NOT NULL")
            teaching_columns = {row[1] for row in conn.execute("PRAGMA table_info(teaching_sessions)").fetchall()}
            if "course_id" not in teaching_columns:
                conn.execute("ALTER TABLE teaching_sessions ADD COLUMN course_id TEXT")
            generated_columns = {row[1] for row in conn.execute("PRAGMA table_info(generated_items)").fetchall()}
            if "calibration_bucket_key" not in generated_columns:
                conn.execute("ALTER TABLE generated_items ADD COLUMN calibration_bucket_key TEXT")
            self.fts_available = False
            try:
                conn.executescript(FTS_SCHEMA)
                self.fts_available = True
                # Backfill any chunks missing from FTS. Checking only for an
                # entirely empty FTS table is insufficient after a runtime moves
                # from an SQLite build without FTS5 to one with FTS5 and then adds
                # newer indexed chunks: older rows would otherwise remain silently
                # invisible whenever FTS returns some candidates.
                conn.execute(
                    """
                    INSERT INTO knowledge_fts(chunk_id, content, topic, unit)
                    SELECT k.chunk_id, k.content, coalesce(k.topic,''), coalesce(k.unit,'')
                    FROM knowledge_chunks k
                    WHERE NOT EXISTS (
                      SELECT 1 FROM knowledge_fts f WHERE f.chunk_id=k.chunk_id
                    )
                    """
                )
            except sqlite3.OperationalError as exc:
                # FTS5 is optional in some embedded SQLite builds. Core persistence
                # must still initialize; retrieval will fall back to LIKE ranking.
                if "fts5" not in str(exc).lower() and "no such module" not in str(exc).lower():
                    raise
                self.fts_available = False
            conn.execute("PRAGMA user_version = 3")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.transaction() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute(sql, params).fetchall())

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        with self.transaction() as conn:
            conn.executemany(sql, rows)

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def loads(value: str | None, default: Any) -> Any:
        if value is None or value == "":
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
