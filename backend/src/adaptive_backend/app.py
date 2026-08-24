from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .adaptive import AdaptiveEngine
from .agents import (CarlAgent, IrisAgent, MemoryCuratorAgent, NavigatorAgent, SamAgent, TravAgent, TutorAgent, XAgent, SubjectAgentFactory)
from .auth import AuthService
from .config import Settings
from .causal_gap import CausalGapEngine
from .database import Database
from .documents import DocumentService, CourseGraphService
from .jobs import JobService
from .mcp_server import MCPService
from .mcp_protocol import build_mcp_protocol
from .learning import LearnerEngine
from .memory import MemoryService
from .models import ModelGateway
from .ocr import OCRService
from .retrieval import RetrievalService
from .rlm import HybridKnowledgeEngine
from .schemas import (
    AdaptiveDecision, AdaptiveNextRequest, AdaptiveState, CodeAnalysisRequest,
    CodeAnalysisResult, EvidenceEvent, HealthResponse, KnowledgeChunkIn, KnowledgeHit,
    KnowledgeSearchRequest, LearningMap, MemoryDeleteRequest, OCRResult, SessionRecord,
    SessionStart, SpeechTranscript, SubjectPackIn, TheoryAnswerRequest, TutorRequest,
    TutorResponse,
)
from .speech import SpeechService, VoiceCallService
from .storage import LocalStorage
from .teaching import TeachingService
from .api_v1 import router as v1_router
from .subjects import CodeReviewService, SubjectRegistry, TheoryService, seed_biology, seed_dsa, seed_builtin_catalog


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.initialize()
        self.models = ModelGateway(settings)
        self.memory = MemoryService(
            self.db, compaction_session_threshold=settings.memory_compaction_session_threshold
        )
        self.retrieval = RetrievalService(self.db)
        self.knowledge = HybridKnowledgeEngine(
            self.retrieval, self.models, enabled=settings.rlm_enabled,
            max_depth=settings.rlm_max_depth, max_calls=settings.rlm_max_calls,
            context_chars=settings.rlm_context_chars,
        )
        self.learner = LearnerEngine(self.db, self.memory)
        self.adaptive = AdaptiveEngine(self.db, self.memory, self.learner, settings)
        self.subjects = SubjectRegistry(self.db)
        self.code = CodeReviewService(self.db, settings)
        self.theory = TheoryService(self.db, self.retrieval, self.models)
        self.ocr = OCRService(settings)
        self.speech = SpeechService(settings)
        self.navigator = NavigatorAgent(self.learner, self.adaptive, self.memory, self.models)
        self.tutor = TutorAgent(self.memory, self.retrieval, self.models)
        self.curator = MemoryCuratorAgent(self.learner, self.adaptive, self.memory)
        self.auth = AuthService(self.db, settings)
        self.jobs = JobService(self.db)
        self.storage = LocalStorage(settings.storage_dir)
        self.documents = DocumentService(
            self.db, self.storage, self.jobs, self.ocr, self.models,
            max_upload_bytes=settings.max_upload_bytes,
            max_document_pages=settings.max_document_pages,
            max_extracted_chars=settings.max_extracted_chars,
        )
        self.course_graphs = CourseGraphService(self.db, self.models, self.knowledge)
        self.subject_agents = SubjectAgentFactory(self.db)
        self.sam = SamAgent(self.learner, self.adaptive, self.memory)
        self.carl = CarlAgent()
        self.trav = TravAgent(self.retrieval, self.knowledge)
        self.causal_gaps = CausalGapEngine(self.db)
        self.iris = IrisAgent(self.causal_gaps)
        self.x = XAgent(
            self.sam, self.carl, self.trav, self.iris,
            max_total_specialists=settings.agent_max_total_specialists,
        )
        self.teaching = TeachingService(
            self.db, self.memory, self.curator, self.models, self.x, self.code, self.subject_agents,
            memory_mode=settings.memory_mode,
        )
        self.voice_calls = VoiceCallService(
            self.db, self.speech, self.teaching, max_call_minutes=settings.voice_max_call_minutes
        )
        self.mcp = MCPService(
            self.db, self.auth, self.memory, self.retrieval, self.teaching,
            context_ttl_seconds=settings.mcp_context_ttl_seconds,
            public_base_url=settings.public_base_url,
        )

    def seed_demo_domains(self) -> None:
        seed_dsa(self.db)
        seed_biology(self.db, self.retrieval)
        seed_builtin_catalog(self.subjects)


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization", "")
    if not raw.lower().startswith("bearer "):
        return None
    return raw[7:].strip() or None


def _loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


async def _read_upload_limited(file: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Upload exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    services = Services(settings)
    if settings.seed_demo_domains:
        services.seed_demo_domains()

    mcp_bundle = build_mcp_protocol(services)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = services
        if mcp_bundle is not None:
            mcp_server, _mcp_asgi = mcp_bundle
            async with mcp_server.session_manager.run():
                yield
        else:
            yield

    if settings.auth_required and (not settings.auth_secret or len(settings.auth_secret) < 32):
        raise ValueError("OVAEL_AUTH_SECRET must be at least 32 characters when OVAEL_AUTH_REQUIRED=true")

    app = FastAPI(
        title="OVAEL Backend",
        version=__version__,
        description="OVAEL adaptive learning, gap detection, dynamic teaching, memory and MCP backend.",
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.mcp_protocol_available = mcp_bundle is not None

    if settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID", "X-CSRF-Token"],
        )

    @app.middleware("http")
    async def security_and_request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid4())
        request.state.request_id = request_id[:128]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), payment=()"
        return response

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        path = request.url.path
        if path in {"/health", "/v1/health"} or path.startswith("/v1/auth/") or path.startswith("/mcp") or settings.debug:
            return await call_next(request)
        # New /v1 application routes authenticate at the route boundary using
        # OVAEL user sessions. Legacy API-key middleware is kept only for the
        # compatibility surface.
        if path.startswith("/v1/"):
            return await call_next(request)
        token = _bearer(request)
        if settings.api_key or settings.admin_api_key:
            # An administrator token is also a valid general-access credential.
            # Without this, configuring distinct API/admin keys makes admin routes
            # unreachable because middleware rejects the admin token before the
            # route-level admin check can see it.
            general_ok = bool(
                token and settings.api_key and hmac.compare_digest(token, settings.api_key)
            )
            admin_ok = bool(
                token
                and settings.admin_api_key
                and hmac.compare_digest(token, settings.admin_api_key)
            )
            if not (general_ok or admin_ok):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        elif not settings.allow_remote_without_api_key and not _loopback(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "Remote access requires ADAPTIVE_API_KEY"},
            )
        return await call_next(request)

    def require_admin(request: Request) -> None:
        if settings.debug:
            return
        if settings.admin_api_key:
            token = _bearer(request)
            if not token or not hmac.compare_digest(token, settings.admin_api_key):
                raise HTTPException(status_code=403, detail="Admin authorization required")
        elif not _loopback(request):
            # Trusted curriculum mutation is more privileged than ordinary tutor
            # access. Without a dedicated admin key it remains loopback-only.
            raise HTTPException(status_code=403, detail="Admin mutation is loopback-only unless ADAPTIVE_ADMIN_API_KEY is configured")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            version=__version__, database="sqlite-local",
            tutor_api_configured=services.models.tutor_available,
            local_model_configured=services.models.local_available,
            supervisor_configured=services.models.supervisor_available,
        )

    # ----- subjects / trusted knowledge (administrative mutation) -----
    @app.get("/subjects")
    def list_subjects() -> list[dict[str, Any]]:
        return services.subjects.list_subjects()

    @app.post("/subjects/packs")
    def upsert_subject_pack(payload: SubjectPackIn, request: Request) -> dict[str, Any]:
        require_admin(request)
        try:
            # Curriculum parameters/edges and learner-state updates share the
            # single-writer lock. After a trusted pack mutation, replay retained
            # evidence so derived BKT/adaptive state is not left under stale
            # model parameters.
            with services.memory.write_lock:
                result = services.subjects.upsert_pack(payload)
                result["replayed_users"] = services.curator.rebuild_subject_after_curriculum_update(
                    payload.subject_id
                )
                return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/knowledge/chunks")
    def add_knowledge(chunk: KnowledgeChunkIn, request: Request) -> dict[str, str]:
        require_admin(request)
        try:
            return {"chunk_id": services.retrieval.upsert_chunk(chunk)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/knowledge/search", response_model=list[KnowledgeHit])
    def knowledge_search(payload: KnowledgeSearchRequest) -> list[KnowledgeHit]:
        return services.retrieval.search(
            query=payload.query, subject_id=payload.subject_id,
            concept_ids=payload.concept_ids, limit=payload.limit, verified_only=True,
        )

    # ----- sessions / learner evidence -----
    @app.post("/sessions", response_model=SessionRecord)
    def start_session(payload: SessionStart) -> SessionRecord:
        try:
            return services.memory.start_session(
                user_id=payload.user_id, subject_id=payload.subject_id,
                goal=payload.goal, source=payload.source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/sessions/{session_id}/complete")
    def complete_session(session_id: str) -> dict[str, Any]:
        try:
            return services.curator.close_session(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/events")
    def record_event(event: EvidenceEvent) -> dict[str, Any]:
        try:
            states, diagnosis, adaptive = services.curator.record(event)
            return {
                "states": [state.model_dump(mode="json") for state in states],
                "diagnosis": diagnosis.model_dump(mode="json"),
                "adaptive": adaptive.model_dump(mode="json"),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/theory/answer")
    def theory_answer(payload: TheoryAnswerRequest) -> dict[str, Any]:
        try:
            evaluation, event = services.theory.evaluate(payload)
            states, diagnosis, adaptive = services.curator.record(event)
            return {
                "evaluation": evaluation.model_dump(mode="json"),
                "states": [state.model_dump(mode="json") for state in states],
                "diagnosis": diagnosis.model_dump(mode="json"),
                "adaptive": adaptive.model_dump(mode="json"),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ----- DSA/code evidence -----
    @app.post("/code/analyze", response_model=CodeAnalysisResult)
    def analyze_code(
        payload: CodeAnalysisRequest,
        run_tests: bool = Query(default=False),
        record: bool = Query(default=True),
    ) -> CodeAnalysisResult:
        try:
            result = services.code.analyze(payload, run_tests=run_tests)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not record:
            return result

        concept_ids = [payload.concept_id, *result.candidate_concepts.keys()]
        valid: list[str] = []
        for cid in dict.fromkeys(concept_ids):
            row = services.db.fetchone("SELECT subject_id FROM concepts WHERE concept_id=?", (cid,))
            if row and row["subject_id"] == payload.subject_id:
                valid.append(cid)
        if payload.concept_id not in valid:
            raise HTTPException(status_code=400, detail="No valid primary concept mapping for code event")

        test_status = result.test_summary.get("status")
        failed = result.test_summary.get("failed")
        correct: bool | None = None
        if test_status == "ok" and isinstance(failed, int):
            correct = failed == 0
        # A syntax failure is evidence about syntax, not proof that the learner
        # lacks the requested algorithmic concept. Keep target correctness unknown.

        problem_row = None
        problem_metadata: dict[str, Any] = {}
        if payload.problem_id:
            problem_row = services.db.fetchone(
                "SELECT subject_id,difficulty,metadata_json FROM questions WHERE question_id=?",
                (payload.problem_id,),
            )
            if not problem_row or problem_row["subject_id"] != payload.subject_id:
                raise HTTPException(status_code=400, detail="Problem does not belong to active subject")
            raw = services.db.loads(problem_row["metadata_json"], {})
            problem_metadata = raw if isinstance(raw, dict) else {}

        try:
            irt_disc = float(problem_metadata.get("irt_discrimination", 1.0))
        except (TypeError, ValueError):
            irt_disc = 1.0
        score = None
        if test_status == "ok":
            try:
                passed_n = max(0.0, float(result.test_summary.get("passed", 0)))
                failed_n = max(0.0, float(result.test_summary.get("failed", 0)))
                score = passed_n / max(1.0, passed_n + failed_n)
            except (TypeError, ValueError):
                score = None

        event = EvidenceEvent(
            session_id=payload.session_id, user_id=payload.user_id,
            subject_id=payload.subject_id, concept_ids=valid,
            primary_concept_id=payload.concept_id, activity_type="code_submission",
            input_mode="code", correct=correct, score=score,
            evidence_strength=(
                1.0 if run_tests and test_status == "ok"
                else 0.0 if not result.compile_ok
                else 0.70
            ),
            response_text=None,
            mistake_type=("syntax_error" if not result.compile_ok else (result.static_patterns[0] if result.static_patterns else None)),
            concept_likelihoods={k: v for k, v in result.candidate_concepts.items() if k in valid},
            metadata={
                "problem_id": payload.problem_id,
                "question_difficulty": float(problem_row["difficulty"]) if problem_row else 0.5,
                "irt_discrimination": irt_disc,
                "behavior": payload.behavior.model_dump(mode="json") if payload.behavior else {},
                "static_patterns": result.static_patterns,
                "test_summary": result.test_summary,
                "concept_strengths": {cid: 0.55 for cid in valid if cid != payload.concept_id},
            },
        )
        try:
            services.curator.record(event)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    # ----- adaptive diagnosis / tutor -----
    @app.get("/adaptive/state/{user_id}/{subject_id}/{concept_id}", response_model=AdaptiveState)
    def adaptive_state(user_id: str, subject_id: str, concept_id: str) -> AdaptiveState:
        try:
            return services.adaptive.get_state(user_id, subject_id, concept_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/adaptive/next", response_model=AdaptiveDecision)
    def adaptive_next(payload: AdaptiveNextRequest) -> AdaptiveDecision:
        try:
            diagnosis = services.learner.diagnose(payload.user_id, payload.concept_id)
            return services.adaptive.recommend_next(
                user_id=payload.user_id, subject_id=payload.subject_id,
                concept_id=payload.concept_id, diagnosis=diagnosis,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/diagnosis/{user_id}/{concept_id}")
    def diagnosis(user_id: str, concept_id: str) -> dict[str, Any]:
        try:
            return services.learner.diagnose(user_id, concept_id).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tutor", response_model=TutorResponse)
    def tutor(payload: TutorRequest) -> TutorResponse:
        try:
            return services.tutor.respond(payload, services.navigator.plan(payload))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/questions/{question_id}")
    def get_question(question_id: str) -> dict[str, Any]:
        question = services.learner.get_question(question_id)
        if not question:
            raise HTTPException(status_code=404, detail="Question not found")
        data = question.model_dump(mode="json")
        data.pop("answer_key", None)
        data.pop("diagnostic_model", None)
        return data

    # ----- derived memory views and privacy controls -----
    @app.get("/memory/map", response_model=LearningMap)
    def memory_map(user_id: str = "local", subject_id: str | None = None) -> LearningMap:
        return services.memory.learning_map(user_id, subject_id)

    # Raw event memory is deliberately not a public API. It remains an internal
    # evidence store; users receive the safe derived map and explicit delete controls.
    @app.post("/memory/personalization/{enabled}")
    def set_personalization(enabled: bool, user_id: str = "local") -> dict[str, Any]:
        services.memory.set_personalization(user_id, enabled)
        return {"user_id": user_id, "personalization_enabled": enabled}

    @app.delete("/memory")
    def delete_memory(payload: MemoryDeleteRequest) -> dict[str, Any]:
        try:
            affected = services.curator.delete_and_rebuild(
                user_id=payload.user_id, subject_id=payload.subject_id,
                start_at=payload.start_at, end_at=payload.end_at,
                delete_all=payload.delete_all,
            )
            return {"deleted": True, "affected_concepts": sorted(affected)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ----- backend multimodal adapters -----
    @app.post("/ocr", response_model=OCRResult)
    async def ocr(
        file: UploadFile = File(...),
        language: str = Query(default="eng", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_+\-]+$"),
    ) -> OCRResult:
        data = await _read_upload_limited(file, services.ocr.MAX_BYTES)
        try:
            return services.ocr.extract(data, language=language)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/speech/transcribe", response_model=SpeechTranscript)
    async def transcribe(
        file: UploadFile = File(...),
        language: str | None = Query(default=None, max_length=32, pattern=r"^[A-Za-z0-9_+\-]*$"),
    ) -> SpeechTranscript:
        data = await _read_upload_limited(file, services.speech.MAX_AUDIO_BYTES)
        try:
            return services.speech.transcribe(data, filename=file.filename or "audio.wav", language=language)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/speech/synthesize")
    def synthesize(
        text: str = Query(min_length=1, max_length=12_000),
        voice: str | None = Query(default=None, max_length=128),
    ) -> Response:
        try:
            return Response(content=services.speech.synthesize(text, voice), media_type="audio/mpeg")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    if mcp_bundle is not None:
        _mcp_server, mcp_asgi = mcp_bundle
        app.mount("/mcp", mcp_asgi)
    app.include_router(v1_router)
    return app
