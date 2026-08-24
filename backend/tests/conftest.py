from __future__ import annotations

from pathlib import Path

import pytest

from adaptive_backend.config import Settings
from adaptive_backend.database import Database
from adaptive_backend.memory import MemoryService
from adaptive_backend.retrieval import RetrievalService
from adaptive_backend.subjects import seed_biology, seed_dsa
from adaptive_backend.learning import LearnerEngine


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        tutor_url=None,
        tutor_model=None,
        tutor_api_key=None,
        local_model_url=None,
        local_model_name="test-local",
        local_model_api_key=None,
        supervisor_url=None,
        supervisor_model=None,
        supervisor_api_key=None,
        allow_local_code_execution=False,
        external_sandbox_url=None,
        whisper_cpp_bin=None,
        whisper_cpp_model=None,
        stt_url=None,
        stt_model=None,
        tts_url=None,
        tts_model=None,
        request_timeout_seconds=10,
        memory_compaction_session_threshold=10,
        adaptive_target_success=0.65,
        adaptive_micro_stage_size=3,
        adaptive_max_difficulty_step=0.18,
        debug=True,
        storage_dir=tmp_path / "uploads",
    )


@pytest.fixture
def core(settings: Settings):
    db = Database(settings.db_path)
    db.initialize()
    retrieval = RetrievalService(db)
    seed_dsa(db)
    seed_biology(db, retrieval)
    memory = MemoryService(db, 10)
    learner = LearnerEngine(db, memory)
    return db, retrieval, memory, learner
