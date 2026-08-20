from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(slots=True, frozen=True)
class Settings:
    db_path: Path
    tutor_url: str | None
    tutor_model: str | None
    tutor_api_key: str | None
    local_model_url: str | None
    local_model_name: str
    local_model_api_key: str | None
    supervisor_url: str | None
    supervisor_model: str | None
    supervisor_api_key: str | None
    allow_local_code_execution: bool
    external_sandbox_url: str | None
    whisper_cpp_bin: str | None
    whisper_cpp_model: str | None
    stt_url: str | None
    stt_model: str | None
    tts_url: str | None
    tts_model: str | None
    request_timeout_seconds: int
    memory_compaction_session_threshold: int
    adaptive_target_success: float
    adaptive_micro_stage_size: int
    adaptive_max_difficulty_step: float
    debug: bool
    # Access control is optional only for a loopback-only local deployment.
    api_key: str | None = None
    admin_api_key: str | None = None
    allow_remote_without_api_key: bool = False
    seed_demo_domains: bool = True
    # OVAEL v0.01 additions. Defaults keep the audited local prototype usable
    # without requiring secrets or cloud services during tests.
    auth_required: bool = False
    auth_secret: str | None = None
    access_token_minutes: int = 60
    session_days: int = 14
    mimo_base_url: str | None = None
    mimo_model: str | None = None
    mimo_api_key: str | None = None
    mcp_context_ttl_seconds: int = 900
    storage_dir: Path = Path("./data/uploads")
    max_upload_bytes: int = 100 * 1024 * 1024
    public_base_url: str = "http://localhost:8000"
    allowed_origins: tuple[str, ...] = ()
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1", "testserver")
    cookie_secure: bool = False
    max_document_pages: int = 4000
    max_extracted_chars: int = 20_000_000
    agent_max_total_specialists: int = 12
    memory_mode: str = "local_first"

    def __post_init__(self) -> None:
        if self.request_timeout_seconds < 1:
            raise ValueError("request_timeout_seconds must be >= 1")
        if self.memory_compaction_session_threshold < 10:
            raise ValueError("memory_compaction_session_threshold must be >= 10")
        if not 0.50 <= self.adaptive_target_success <= 0.85:
            raise ValueError("adaptive_target_success must be in [0.50, 0.85]")
        if self.adaptive_micro_stage_size < 1:
            raise ValueError("adaptive_micro_stage_size must be >= 1")
        if not 0.01 <= self.adaptive_max_difficulty_step <= 0.50:
            raise ValueError("adaptive_max_difficulty_step must be in [0.01, 0.50]")
        if self.access_token_minutes < 5:
            raise ValueError("access_token_minutes must be >= 5")
        if self.session_days < 1:
            raise ValueError("session_days must be >= 1")
        if self.mcp_context_ttl_seconds < 60:
            raise ValueError("mcp_context_ttl_seconds must be >= 60")
        if self.max_upload_bytes < 1024 * 1024:
            raise ValueError("max_upload_bytes must be >= 1 MiB")
        if self.max_document_pages < 1 or self.max_document_pages > 20_000:
            raise ValueError("max_document_pages must be in [1, 20000]")
        if self.max_extracted_chars < 100_000:
            raise ValueError("max_extracted_chars must be >= 100000")
        if not 0 <= self.agent_max_total_specialists <= 12:
            raise ValueError("agent_max_total_specialists must be in [0, 12]")
        if self.memory_mode not in {"local_first", "server"}:
            raise ValueError("memory_mode must be 'local_first' or 'server'")
        for name, value in (("api_key", self.api_key), ("admin_api_key", self.admin_api_key)):
            if value is not None and len(value) < 16:
                raise ValueError(f"{name} must be at least 16 characters when configured")

    @classmethod
    def from_env(cls) -> "Settings":
        db = Path(os.getenv("ADAPTIVE_DB_PATH", "./data/adaptive_learning.db")).expanduser()
        return cls(
            db_path=db,
            tutor_url=os.getenv("ADAPTIVE_TUTOR_URL") or None,
            tutor_model=os.getenv("ADAPTIVE_TUTOR_MODEL") or None,
            tutor_api_key=os.getenv("ADAPTIVE_TUTOR_API_KEY") or None,
            local_model_url=os.getenv("ADAPTIVE_LOCAL_MODEL_URL") or None,
            local_model_name=os.getenv("ADAPTIVE_LOCAL_MODEL_NAME", "Qwen3-1.7B"),
            local_model_api_key=os.getenv("ADAPTIVE_LOCAL_MODEL_API_KEY") or None,
            supervisor_url=os.getenv("ADAPTIVE_SUPERVISOR_URL") or None,
            supervisor_model=os.getenv("ADAPTIVE_SUPERVISOR_MODEL") or None,
            supervisor_api_key=os.getenv("ADAPTIVE_SUPERVISOR_API_KEY") or None,
            allow_local_code_execution=_bool("ADAPTIVE_ALLOW_LOCAL_CODE_EXECUTION", False),
            external_sandbox_url=os.getenv("ADAPTIVE_EXTERNAL_SANDBOX_URL") or None,
            whisper_cpp_bin=os.getenv("ADAPTIVE_WHISPER_CPP_BIN") or None,
            whisper_cpp_model=os.getenv("ADAPTIVE_WHISPER_CPP_MODEL") or None,
            stt_url=os.getenv("ADAPTIVE_STT_URL") or None,
            stt_model=os.getenv("ADAPTIVE_STT_MODEL") or None,
            tts_url=os.getenv("ADAPTIVE_TTS_URL") or None,
            tts_model=os.getenv("ADAPTIVE_TTS_MODEL") or None,
            request_timeout_seconds=_int("ADAPTIVE_REQUEST_TIMEOUT_SECONDS", 90),
            memory_compaction_session_threshold=_int(
                "ADAPTIVE_MEMORY_COMPACTION_SESSION_THRESHOLD", 40
            ),
            adaptive_target_success=_float("ADAPTIVE_TARGET_SUCCESS", 0.65),
            adaptive_micro_stage_size=_int("ADAPTIVE_MICRO_STAGE_SIZE", 3),
            adaptive_max_difficulty_step=_float("ADAPTIVE_MAX_DIFFICULTY_STEP", 0.08),
            debug=_bool("ADAPTIVE_DEBUG", False),
            api_key=os.getenv("ADAPTIVE_API_KEY") or None,
            admin_api_key=os.getenv("ADAPTIVE_ADMIN_API_KEY") or None,
            allow_remote_without_api_key=_bool("ADAPTIVE_ALLOW_REMOTE_WITHOUT_API_KEY", False),
            seed_demo_domains=_bool("ADAPTIVE_SEED_DEMO_DOMAINS", True),
            auth_required=_bool("OVAEL_AUTH_REQUIRED", False),
            auth_secret=os.getenv("OVAEL_AUTH_SECRET") or None,
            access_token_minutes=_int("OVAEL_ACCESS_TOKEN_MINUTES", 60),
            session_days=_int("OVAEL_SESSION_DAYS", 14),
            mimo_base_url=(
                os.getenv("OVAEL_MIMO_BASE_URL")
                or os.getenv("PS10_MIMO_BASE_URL")
                or None
            ),
            mimo_model=(
                os.getenv("OVAEL_MIMO_MODEL")
                or os.getenv("PS10_MIMO_MODEL")
                or None
            ),
            mimo_api_key=(
                os.getenv("OVAEL_MIMO_API_KEY")
                or os.getenv("PS10_MIMO_API_KEY")
                or None
            ),
            mcp_context_ttl_seconds=_int("OVAEL_MCP_CONTEXT_TTL_SECONDS", 900),
            storage_dir=Path(os.getenv("OVAEL_STORAGE_DIR", "./data/uploads")).expanduser(),
            max_upload_bytes=_int("OVAEL_MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
            public_base_url=os.getenv("OVAEL_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
            allowed_origins=_csv("OVAEL_ALLOWED_ORIGINS", ()),
            trusted_hosts=_csv("OVAEL_TRUSTED_HOSTS", ("localhost", "127.0.0.1", "::1", "testserver")),
            cookie_secure=_bool("OVAEL_COOKIE_SECURE", False),
            max_document_pages=_int("OVAEL_MAX_DOCUMENT_PAGES", 4000),
            max_extracted_chars=_int("OVAEL_MAX_EXTRACTED_CHARS", 20_000_000),
            agent_max_total_specialists=_int("OVAEL_AGENT_MAX_TOTAL_SPECIALISTS", 12),
            memory_mode=(os.getenv("OVAEL_MEMORY_MODE", "local_first").strip().lower()),
        )
