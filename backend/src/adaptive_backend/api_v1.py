from __future__ import annotations

from io import BytesIO
import json
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from .auth import AuthError, AuthPrincipal
from .mcp_server import DEFAULT_SCOPES, MCPAuthError
from .teaching import LearningSessionStart, LearningTurnInput

router = APIRouter()


def _services(request: Request):
    return request.app.state.services


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip() or None
    return None


def principal(request: Request) -> AuthPrincipal:
    services = _services(request)
    bearer = _bearer(request)
    cookie_token = request.cookies.get("ovael_session")
    token = bearer or cookie_token
    if not token:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "Sign in required"})

    # Browser-cookie authentication uses a double-submit CSRF token for unsafe
    # methods. API/CLI bearer calls are not subject to browser CSRF. SameSite=Lax
    # remains an additional browser boundary, not the sole defense.
    if not bearer and cookie_token and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        csrf_cookie = request.cookies.get("ovael_csrf") or ""
        csrf_header = request.headers.get("x-csrf-token") or ""
        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(status_code=403, detail={"code": "CSRF_FAILED", "message": "CSRF token is missing or invalid"})
    try:
        return services.auth.authenticate_token(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID", "message": str(exc)}) from exc


def _set_auth_cookies(
    response: Response, request: Request, access_token: str, refresh_token: str | None = None
) -> None:
    settings = _services(request).settings
    access_max_age = settings.access_token_minutes * 60
    refresh_max_age = settings.session_days * 24 * 60 * 60
    csrf = secrets.token_urlsafe(24)
    response.set_cookie(
        "ovael_session", access_token, max_age=access_max_age, httponly=True,
        secure=settings.cookie_secure, samesite="lax", path="/",
    )
    if refresh_token:
        response.set_cookie(
            "ovael_refresh", refresh_token, max_age=refresh_max_age, httponly=True,
            secure=settings.cookie_secure, samesite="lax", path="/v1/auth/",
        )
    response.set_cookie(
        "ovael_csrf", csrf, max_age=refresh_max_age, httponly=False,
        secure=settings.cookie_secure, samesite="lax", path="/",
    )


def _verify_cookie_csrf(request: Request) -> None:
    csrf_cookie = request.cookies.get("ovael_csrf") or ""
    csrf_header = request.headers.get("x-csrf-token") or ""
    if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
        raise HTTPException(status_code=403, detail={"code": "CSRF_FAILED", "message": "CSRF token is missing or invalid"})


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie("ovael_session", path="/")
    response.delete_cookie("ovael_refresh", path="/v1/auth/")
    response.delete_cookie("ovael_csrf", path="/")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIn(_Strict):
    username: str
    password: str
    role: str = Field(default="learner", pattern="^(learner|teacher)$")


class LoginIn(_Strict):
    username: str
    password: str


class RefreshIn(_Strict):
    refresh_token: str | None = None


class ChangePasswordIn(_Strict):
    current_password: str
    new_password: str


class RecoverIn(_Strict):
    username: str
    recovery_key: str
    new_password: str


class RotateRecoveryIn(_Strict):
    password: str


class DeleteAccountIn(_Strict):
    password: str


class CourseIn(_Strict):
    name: str = Field(min_length=1, max_length=200)
    subject_id: str | None = Field(default=None, max_length=128)
    subject_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    course_kind: str = Field(default="personal", pattern="^(personal|class)$")


class RelayIn(_Strict):
    capsule: dict[str, Any]


class MCPConnectionIn(_Strict):
    client_name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: sorted(DEFAULT_SCOPES), max_length=20)
    days: int = Field(default=30, ge=1, le=90)


class InboxAckIn(_Strict):
    status: str


class RetrievalIn(_Strict):
    query: str = Field(min_length=1, max_length=4000)
    subject_id: str = Field(min_length=1, max_length=128)
    concept_ids: list[str] = Field(default_factory=list, max_length=32)
    course_id: str | None = None
    limit: int = Field(default=6, ge=1, le=20)


class CausalAnalyzeIn(_Strict):
    subject_id: str | None = Field(default=None, max_length=128)
    max_cases: int = Field(default=6, ge=1, le=12)


class CausalChallengeIn(_Strict):
    reason: str = Field(min_length=1, max_length=500)


class PersonalizationIn(_Strict):
    enabled: bool


class MemoryDeleteIn(_Strict):
    subject_id: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    delete_all: bool = False


class CourseUpdateIn(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)



class EnrollmentIn(_Strict):
    learner_username: str = Field(min_length=3, max_length=64)


class ProgressSnapshotIn(_Strict):
    graph_version: str = Field(min_length=1, max_length=128)
    concept_states: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    gap_summary: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    shared_with_teacher: bool = False


class CourseGraphProposalIn(_Strict):
    document_ids: list[str] = Field(default_factory=list, min_length=1, max_length=20)
    objective: str | None = Field(default=None, max_length=2000)


class CourseGraphEditIn(_Strict):
    graph: dict[str, Any]


class VoiceCallStartIn(_Strict):
    teaching_session_id: str
    language: str | None = Field(default=None, max_length=32)
    voice: str | None = Field(default=None, max_length=64)
    playback_rate: float = Field(default=1.0, ge=0.75, le=3.0)


@router.get("/v1/health")
def v1_health(request: Request) -> dict[str, Any]:
    s = _services(request)
    server_stt = bool(
        (s.settings.whisper_cpp_bin and s.settings.whisper_cpp_model)
        or s.settings.stt_url
        or (s.settings.mimo_base_url and s.settings.mimo_api_key)
    )
    server_tts = bool(
        s.settings.tts_url
        or (s.settings.mimo_base_url and s.settings.mimo_api_key)
    )
    return {
        "status": "ok",
        "product": "OVAEL",
        "version": request.app.version,
        "database": "sqlite-local",
        "mimo_configured": s.models.mimo_available,
        "ocr_configured": bool(s.settings.mimo_api_key),
        "stt_configured": server_stt,
        "tts_configured": server_tts,
        "voice_transport": "turn_based",
        "live_call_supported": False,
        "mcp": "official-sdk" if getattr(request.app.state, "mcp_protocol_available", False) else "legacy-fallback-only",
        "dynamic_teaching": "enabled",
        "agents": ["X", "Sam", "Carl", "Trav", "IRIS"],
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@router.post("/v1/auth/register")
def register(payload: RegisterIn, request: Request, response: Response) -> dict[str, Any]:
    try:
        result = _services(request).auth.register(
            payload.username, payload.password, role=payload.role, user_agent=request.headers.get("user-agent")
        )
        _set_auth_cookies(response, request, result["access_token"], result.get("refresh_token"))
        public = dict(result)
        public.pop("refresh_token", None)
        return public
    except AuthError as exc:
        raise HTTPException(status_code=400, detail={"code": "AUTH_REGISTER_FAILED", "message": str(exc)}) from exc


@router.post("/v1/auth/login")
def login(payload: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    try:
        result = _services(request).auth.login(
            payload.username, payload.password, user_agent=request.headers.get("user-agent")
        )
        _set_auth_cookies(response, request, result["access_token"], result.get("refresh_token"))
        public = dict(result)
        public.pop("refresh_token", None)
        return public
    except AuthError as exc:
        raise HTTPException(status_code=401, detail={"code": "AUTH_LOGIN_FAILED", "message": str(exc)}) from exc


@router.post("/v1/auth/refresh")
def refresh_auth(payload: RefreshIn, request: Request, response: Response) -> dict[str, Any]:
    cookie_refresh = request.cookies.get("ovael_refresh")
    refresh_token = payload.refresh_token or cookie_refresh
    if cookie_refresh and not payload.refresh_token:
        _verify_cookie_csrf(request)
    try:
        result = _services(request).auth.refresh(refresh_token or "")
        _set_auth_cookies(response, request, result["access_token"], result.get("refresh_token"))
        public = dict(result)
        public.pop("refresh_token", None)
        return public
    except AuthError as exc:
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail={"code": "AUTH_REFRESH_FAILED", "message": str(exc)}) from exc


@router.get("/v1/auth/me")
def me(request: Request) -> dict[str, str]:
    p = principal(request)
    return {"user_id": p.user_id, "username": p.username, "role": p.role}


@router.get("/v1/profile")
def get_profile(request: Request) -> dict[str, Any]:
    """Return the authenticated learner/teacher profile projection.

    The profile is deliberately separate from the learner-memory graph. It holds
    user-facing identity/preferences only; mastery and gap state remain in the
    learning-memory subsystem.
    """
    p = principal(request)
    db = _services(request).db
    row = db.fetchone(
        "SELECT display_name,institution,preferences_json,synthetic,updated_at FROM learner_profiles WHERE user_id=?",
        (p.user_id,),
    )
    if not row:
        return {
            "user_id": p.user_id,
            "username": p.username,
            "role": p.role,
            "display_name": p.username,
            "institution": None,
            "preferences": {},
            "synthetic": False,
            "updated_at": None,
        }
    return {
        "user_id": p.user_id,
        "username": p.username,
        "role": p.role,
        "display_name": row["display_name"] or p.username,
        "institution": row["institution"],
        "preferences": db.loads(row["preferences_json"], {}),
        "synthetic": bool(row["synthetic"]),
        "updated_at": row["updated_at"],
    }


@router.post("/v1/auth/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    p = principal(request)
    _services(request).auth.logout(p)
    _clear_auth_cookies(response)
    return {"logged_out": True}


@router.post("/v1/auth/logout-all")
def logout_all(request: Request, response: Response) -> dict[str, int]:
    p = principal(request)
    count = _services(request).auth.logout_all(p)
    _clear_auth_cookies(response)
    return {"revoked_sessions": count}


@router.post("/v1/auth/change-password")
def change_password(payload: ChangePasswordIn, request: Request) -> dict[str, bool]:
    p = principal(request)
    try:
        _services(request).auth.change_password(p, payload.current_password, payload.new_password)
        return {"changed": True}
    except AuthError as exc:
        raise HTTPException(status_code=400, detail={"code": "PASSWORD_CHANGE_FAILED", "message": str(exc)}) from exc


@router.post("/v1/auth/recover")
def recover(payload: RecoverIn, request: Request) -> dict[str, str]:
    try:
        return _services(request).auth.recover(payload.username, payload.recovery_key, payload.new_password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail={"code": "RECOVERY_FAILED", "message": str(exc)}) from exc


@router.post("/v1/auth/recovery-key")
def rotate_recovery(payload: RotateRecoveryIn, request: Request) -> dict[str, str]:
    p = principal(request)
    try:
        return {"recovery_key": _services(request).auth.rotate_recovery_key(p, payload.password)}
    except AuthError as exc:
        raise HTTPException(status_code=400, detail={"code": "RECOVERY_ROTATE_FAILED", "message": str(exc)}) from exc


@router.get("/v1/auth/sessions")
def auth_sessions(request: Request) -> list[dict[str, Any]]:
    p = principal(request)
    return _services(request).auth.list_sessions(p)


@router.delete("/v1/auth/sessions/{session_id}")
def revoke_auth_session(session_id: str, request: Request) -> dict[str, bool]:
    p = principal(request)
    return {"revoked": _services(request).auth.revoke_session(p, session_id)}


@router.delete("/v1/auth/account")
def delete_account(payload: DeleteAccountIn, request: Request, response: Response) -> dict[str, Any]:
    p = principal(request)
    s = _services(request)
    try:
        storage_keys = s.auth.delete_account(p, payload.password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail={"code": "ACCOUNT_DELETE_FAILED", "message": str(exc)}) from exc
    cleaned = 0
    for key in storage_keys:
        try:
            s.storage.delete(key); cleaned += 1
        except Exception:
            # Account data is already gone. A deployment object-lifecycle policy
            # should catch any rare storage orphan that cannot be deleted now.
            pass
    _clear_auth_cookies(response)
    return {"deleted": True, "storage_objects_cleaned": cleaned, "storage_objects_total": len(storage_keys)}


# ---------------------------------------------------------------------------
# Subjects / courses
# ---------------------------------------------------------------------------
@router.get("/v1/subjects")
def subjects(request: Request) -> list[dict[str, Any]]:
    p = principal(request); s = _services(request)
    rows = s.subjects.list_subjects()
    accessible = {r["subject_id"] for r in s.db.fetchall(
        """SELECT DISTINCT c.subject_id FROM courses c LEFT JOIN course_enrollments e
           ON e.course_id=c.course_id AND e.learner_user_id=? AND e.status='active'
           WHERE c.owner_user_id=? OR e.learner_user_id=?""",
        (p.user_id, p.user_id, p.user_id),
    )}
    return [row for row in rows if not str(row["subject_id"]).startswith("USR:") or row["subject_id"] in accessible]


@router.post("/v1/courses")
def create_course(payload: CourseIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    s = _services(request)
    if payload.course_kind == "class" and p.role != "teacher":
        raise HTTPException(status_code=403, detail={"code": "TEACHER_REQUIRED", "message": "Class courses require a teacher account"})
    subject_id = payload.subject_id
    if subject_id:
        if not s.db.fetchone("SELECT 1 FROM subjects WHERE subject_id=?", (subject_id,)):
            raise HTTPException(status_code=400, detail="Unknown subject")
    else:
        # Unlimited runtime subjects: a new course can create its own isolated
        # subject namespace without adding Python source files.
        subject_id = f"USR:{p.user_id[:8]}:{uuid4().hex[:10]}"
        subject_name = (payload.subject_name or payload.name).strip()
        s.db.execute(
            "INSERT INTO subjects(subject_id,name,description) VALUES(?,?,?)",
            (subject_id, subject_name, f"User-created OVAEL subject for {payload.name}"),
        )
    course_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    s.db.execute(
        "INSERT INTO courses(course_id,owner_user_id,subject_id,name,description,visibility,course_kind,created_at,updated_at) VALUES(?,?,?,?,?,'private',?,?,?)",
        (course_id, p.user_id, subject_id, payload.name, payload.description, payload.course_kind, now, now),
    )
    return {
        "course_id": course_id,
        "name": payload.name,
        "subject_id": subject_id,
        "subject_name": payload.subject_name,
        "description": payload.description,
        "course_kind": payload.course_kind,
        "visibility": "private",
        "created_at": now,
    }

@router.get("/v1/courses")
def list_courses(request: Request) -> list[dict[str, Any]]:
    p = principal(request)
    rows = _services(request).db.fetchall(
        """SELECT DISTINCT c.*, CASE WHEN c.owner_user_id=? THEN 'owner' ELSE 'learner' END AS membership
           FROM courses c LEFT JOIN course_enrollments e ON e.course_id=c.course_id AND e.status='active'
           WHERE c.owner_user_id=? OR e.learner_user_id=? ORDER BY c.updated_at DESC""",
        (p.user_id, p.user_id, p.user_id),
    )
    return [dict(r) for r in rows]


@router.get("/v1/courses/{course_id}")
def get_course(course_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    row = _services(request).db.fetchone(
        """SELECT DISTINCT c.* FROM courses c
           LEFT JOIN course_enrollments e ON e.course_id=c.course_id AND e.status='active'
           WHERE c.course_id=? AND (c.owner_user_id=? OR e.learner_user_id=?)""",
        (course_id, p.user_id, p.user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Course not found")
    return dict(row)


@router.patch("/v1/courses/{course_id}")
def update_course(course_id: str, payload: CourseUpdateIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    s = _services(request)
    row = s.db.fetchone("SELECT * FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id))
    if not row:
        raise HTTPException(status_code=404, detail="Course not found")
    if payload.name is None and payload.description is None:
        raise HTTPException(status_code=400, detail="No course fields supplied")
    name = payload.name if payload.name is not None else row["name"]
    description = payload.description if payload.description is not None else row["description"]
    now = datetime.now(timezone.utc).isoformat()
    s.db.execute(
        "UPDATE courses SET name=?,description=?,updated_at=? WHERE course_id=? AND owner_user_id=?",
        (name, description, now, course_id, p.user_id),
    )
    updated = s.db.fetchone("SELECT * FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id))
    return dict(updated)


@router.delete("/v1/courses/{course_id}")
def delete_course(course_id: str, request: Request) -> dict[str, bool]:
    p = principal(request)
    s = _services(request)
    if not s.db.fetchone("SELECT 1 FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id)):
        raise HTTPException(status_code=404, detail="Course not found")
    if s.db.fetchone("SELECT 1 FROM documents WHERE course_id=? AND owner_user_id=? LIMIT 1", (course_id, p.user_id)):
        raise HTTPException(status_code=409, detail="Delete course documents first")
    course = s.db.fetchone("SELECT subject_id FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id))
    subject_id = course["subject_id"] if course else None
    s.db.execute("DELETE FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id))
    if subject_id and str(subject_id).startswith("USR:") and not s.db.fetchone("SELECT 1 FROM courses WHERE subject_id=? LIMIT 1", (subject_id,)):
        # Historical learner sessions may still reference a course-isolated subject.
        # Keep that hidden namespace while it is referenced rather than turning a
        # legitimate course deletion into a foreign-key failure.
        still_referenced = s.db.fetchone("SELECT 1 FROM sessions WHERE subject_id=? LIMIT 1", (subject_id,))
        if not still_referenced:
            s.db.execute("DELETE FROM subjects WHERE subject_id=?", (subject_id,))
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Documents/jobs
# ---------------------------------------------------------------------------
@router.post("/v1/documents/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    course_id: str | None = Query(default=None),
    auto_route: bool = Query(default=False),
) -> dict[str, Any]:
    p = principal(request)
    s = _services(request)
    if course_id and not s.db.fetchone(
        "SELECT 1 FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id)
    ):
        raise HTTPException(status_code=404, detail="Course not found")
    try:
        return await s.documents.upload(
            user_id=p.user_id,
            upload=file,
            course_id=course_id,
            auto_route=auto_route,
        )
    except ValueError as exc:
        status = 413 if "100 MiB" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/v1/documents")
def documents(request: Request, course_id: str | None = None) -> list[dict[str, Any]]:
    p = principal(request)
    return _services(request).documents.list(p.user_id, course_id)


@router.get("/v1/documents/{document_id}")
def document(document_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).documents.get(p.user_id, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/v1/documents/{document_id}/pages/{page_no}")
def document_page(document_id: str, page_no: int, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).documents.page(p.user_id, document_id, page_no)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/documents/{document_id}/route")
def route_document(document_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).documents.route(p.user_id, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/v1/documents/{document_id}")
def delete_document(document_id: str, request: Request) -> dict[str, bool]:
    p = principal(request)
    try:
        _services(request).documents.delete(p.user_id, document_id)
        return {"deleted": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/v1/jobs")
def list_jobs(request: Request) -> list[dict[str, Any]]:
    p = principal(request)
    return _services(request).jobs.list(p.user_id)


@router.get("/v1/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).jobs.get(p.user_id, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).jobs.cancel(p.user_id, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Retrieval / system introspection
# ---------------------------------------------------------------------------
@router.post("/v1/retrieval/search")
def v1_retrieval(payload: RetrievalIn, request: Request) -> list[dict[str, Any]]:
    p = principal(request)
    if payload.course_id and not _services(request).db.fetchone(
        """SELECT 1 FROM courses c LEFT JOIN course_enrollments e
           ON e.course_id=c.course_id AND e.learner_user_id=? AND e.status='active'
           WHERE c.course_id=? AND (c.owner_user_id=? OR e.learner_user_id=?)""",
        (p.user_id, payload.course_id, p.user_id, p.user_id),
    ):
        raise HTTPException(status_code=404, detail="Course not found")
    hits = _services(request).retrieval.search(
        query=payload.query, subject_id=payload.subject_id, concept_ids=payload.concept_ids,
        limit=payload.limit, verified_only=True, user_id=p.user_id, course_id=payload.course_id,
    )
    return [h.model_dump(mode="json") for h in hits]


@router.get("/v1/system/agents")
def system_agents(request: Request) -> dict[str, Any]:
    principal(request)
    return {
        "orchestrator": "X",
        "leads": [
            {"name": "Sam", "role": "learner intelligence and gap diagnosis"},
            {"name": "Carl", "role": "teaching strategy and dynamic assessment"},
            {"name": "Trav", "role": "knowledge, retrieval and tools"},
            {"name": "IRIS", "role": "causal gap forensics and falsifiable repair constraints"},
        ],
        "sub_agents": {
            "max_per_lead": 3,
            "max_total_per_turn": _services(request).settings.agent_max_total_specialists,
            "recursive_spawn": False,
            "execution": "parallel_when_independent",
            "spawn_policy": "evidence_triggered",
        },
    }


# ---------------------------------------------------------------------------
# Isolated causal learning-gap fragment
# ---------------------------------------------------------------------------
@router.post("/v1/gaps/causal/analyze")
def analyze_causal_gaps(payload: CausalAnalyzeIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    s = _services(request)
    if p.role != "learner":
        raise HTTPException(status_code=403, detail="Causal gap forensics is learner-scoped")
    if not s.memory.personalization_enabled(p.user_id):
        raise HTTPException(
            status_code=409,
            detail={"code": "PERSONALIZATION_PAUSED", "message": "Resume personalization to generate a fresh gap analysis."},
        )
    return s.x.forensics(
        user_id=p.user_id,
        subject_id=payload.subject_id,
        max_cases=payload.max_cases,
        persist=True,
    )


@router.get("/v1/gaps/causal")
def list_causal_gaps(request: Request, subject_id: str | None = None) -> dict[str, Any]:
    p = principal(request)
    if p.role != "learner":
        raise HTTPException(status_code=403, detail="Causal gap forensics is learner-scoped")
    return _services(request).causal_gaps.list_cases(user_id=p.user_id, subject_id=subject_id)


@router.get("/v1/gaps/causal/{case_id}")
def get_causal_gap(case_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    case = _services(request).causal_gaps.get_case(user_id=p.user_id, case_id=case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Causal gap case not found")
    return case


@router.post("/v1/gaps/causal/{case_id}/challenge")
def challenge_causal_gap(case_id: str, payload: CausalChallengeIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    case = _services(request).causal_gaps.challenge(user_id=p.user_id, case_id=case_id, reason=payload.reason)
    if not case:
        raise HTTPException(status_code=404, detail="Causal gap case not found")
    return case


@router.get("/v1/gaps/causal/{case_id}/plot.png")
def causal_gap_plot(case_id: str, request: Request) -> Response:
    p = principal(request)
    case = _services(request).causal_gaps.get_case(user_id=p.user_id, case_id=case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Causal gap case not found")
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    graph = case.get("micrograph") or {"nodes": [], "edges": []}
    nodes = graph.get("nodes") or []
    roles: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        roles.setdefault(str(node.get("role")), []).append(node)
    positions: dict[str, tuple[float, float]] = {}
    columns = {"upstream": 0.7, "cause": 2.0, "focus": 3.4, "downstream": 5.1}
    for role, items in roles.items():
        for index, node in enumerate(items):
            y = (len(items) - 1) / 2 - index
            positions[str(node["id"])] = (columns.get(role, 2.0), y)

    fig, ax = plt.subplots(figsize=(10, 4.8), facecolor="#f7f6f1")
    ax.set_facecolor("#f7f6f1")
    for edge in graph.get("edges") or []:
        source = positions.get(str(edge.get("source")))
        target = positions.get(str(edge.get("target")))
        if not source or not target:
            continue
        ax.annotate("", xy=target, xytext=source, arrowprops={"arrowstyle": "->", "color": "#9aa1a6", "lw": 1.2})
    palette = {"upstream": "#d9e3fb", "cause": "#f4dfc7", "focus": "#2859d8", "downstream": "#d8eee7"}
    for node in nodes:
        node_id = str(node.get("id"))
        x, y = positions.get(node_id, (2.0, 0.0))
        role = str(node.get("role"))
        color = palette.get(role, "#e7e7e2")
        text_color = "white" if role == "focus" else "#17202b"
        ax.scatter([x], [y], s=2200 if role == "focus" else 1600, c=[color], edgecolors="#17202b", linewidths=0.8, zorder=3)
        label = str(node.get("label") or node_id)
        if len(label) > 22:
            label = label[:20] + "…"
        ax.text(x, y, label, ha="center", va="center", color=text_color, fontsize=9, weight="semibold", zorder=4, wrap=True)
    ax.set_title(f"IRIS causal working graph · {case.get('concept_name')}", loc="left", fontsize=15, color="#17202b", pad=16)
    ax.text(0.0, -0.08, "Heuristic support, not a diagnosis or calibrated probability", transform=ax.transAxes, fontsize=9, color="#68727b")
    ax.set_xlim(0, 6)
    ax.set_ylim(-2.2, 2.2)
    ax.axis("off")
    fig.tight_layout()
    out = BytesIO()
    fig.savefig(out, format="png", dpi=180, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return Response(
        content=out.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="iris-{case_id}.png"', "Cache-Control": "private, no-store"},
    )


# ---------------------------------------------------------------------------
# Dynamic learning API
# ---------------------------------------------------------------------------
@router.get("/v1/learning/sessions")
def list_learning_sessions(
    request: Request,
    subject_id: str | None = None,
    course_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    p = principal(request)
    clauses = ["owner_user_id=?"]
    params: list[Any] = [p.user_id]
    if subject_id:
        clauses.append("subject_id=?"); params.append(subject_id)
    if course_id:
        clauses.append("course_id=?"); params.append(course_id)
    params.append(limit)
    rows = _services(request).db.fetchall(
        f"SELECT teaching_session_id,subject_id,course_id,concept_id,goal,mode,source_client,status,state_json,created_at,updated_at,ended_at "
        f"FROM teaching_sessions WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
        tuple(params),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        state = _services(request).db.loads(row["state_json"], {})
        out.append({
            "teaching_session_id": row["teaching_session_id"],
            "subject_id": row["subject_id"],
            "course_id": row["course_id"],
            "concept_id": row["concept_id"],
            "goal": row["goal"],
            "mode": row["mode"],
            "source_client": row["source_client"],
            "status": row["status"],
            "turn_count": int(state.get("turn_count", 0)),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "ended_at": row["ended_at"],
        })
    return out


@router.post("/v1/learning/sessions")
def start_learning(payload: LearningSessionStart, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).teaching.start(user_id=p.user_id, request=payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/v1/learning/sessions/{session_id}")
def get_learning(session_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).teaching.get(user_id=p.user_id, session_id=session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/learning/sessions/{session_id}/turns")
def learning_turn(
    session_id: str,
    payload: LearningTurnInput,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).teaching.turn(
            user_id=p.user_id,
            session_id=session_id,
            turn=payload,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1/learning/sessions/{session_id}/complete")
def complete_learning(session_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).teaching.complete(user_id=p.user_id, session_id=session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/v1/memory/settings")
def v1_memory_settings(request: Request) -> dict[str, bool]:
    p = principal(request)
    return {"personalization_enabled": _services(request).memory.personalization_enabled(p.user_id)}


@router.post("/v1/memory/personalization")
def v1_set_personalization(payload: PersonalizationIn, request: Request) -> dict[str, bool]:
    p = principal(request)
    _services(request).memory.set_personalization(p.user_id, payload.enabled)
    if not payload.enabled:
        _services(request).db.execute("DELETE FROM mcp_context_relay WHERE user_id=?", (p.user_id,))
    _services(request).auth.audit(p.user_id, "memory.personalization", metadata={"enabled": payload.enabled})
    return {"personalization_enabled": payload.enabled}


@router.delete("/v1/memory")
def v1_delete_memory(payload: MemoryDeleteIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    if payload.start_at and payload.end_at and payload.start_at > payload.end_at:
        raise HTTPException(status_code=400, detail="start_at must be <= end_at")
    if payload.delete_all and any((payload.subject_id, payload.start_at, payload.end_at)):
        raise HTTPException(status_code=400, detail="delete_all cannot be combined with filters")
    if not payload.delete_all and not any((payload.subject_id, payload.start_at, payload.end_at)):
        raise HTTPException(status_code=400, detail="Specify subject/date filters or delete_all=true")
    affected = _services(request).curator.delete_and_rebuild(
        user_id=p.user_id, subject_id=payload.subject_id,
        start_at=payload.start_at, end_at=payload.end_at, delete_all=payload.delete_all,
    )
    _services(request).auth.audit(
        p.user_id, "memory.delete",
        metadata={"delete_all": payload.delete_all, "subject_id": payload.subject_id, "affected_concepts": len(affected)},
    )
    return {"deleted": True, "affected_concepts": sorted(affected)}


@router.get("/v1/memory/map")
def v1_memory_map(request: Request, subject_id: str | None = None) -> dict[str, Any]:
    p = principal(request)
    graph = _services(request).memory.learning_map(p.user_id, subject_id)
    return graph.model_dump(mode="json")


# ---------------------------------------------------------------------------
# MCP connections/context/inbox
# ---------------------------------------------------------------------------
@router.post("/v1/connections/mcp")
def create_mcp_connection(payload: MCPConnectionIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).mcp.create_connection(
            user_id=p.user_id, client_name=payload.client_name, scopes=payload.scopes, days=payload.days
        )
    except MCPAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/v1/connections/mcp")
def mcp_connections(request: Request) -> list[dict[str, Any]]:
    p = principal(request)
    return _services(request).mcp.list_connections(p.user_id)


@router.delete("/v1/connections/mcp/{connection_id}")
def revoke_mcp(connection_id: str, request: Request) -> dict[str, bool]:
    p = principal(request)
    return {"revoked": _services(request).mcp.revoke_connection(p.user_id, connection_id)}


@router.post("/v1/mcp/context-relay")
def mcp_context(payload: RelayIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).mcp.put_context(p.user_id, payload.capsule)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/v1/mcp/external-inbox")
def external_inbox(request: Request) -> list[dict[str, Any]]:
    p = principal(request)
    return _services(request).mcp.list_inbox(p.user_id)


@router.post("/v1/mcp/external-inbox/{inbox_id}")
def ack_external(inbox_id: str, payload: InboxAckIn, request: Request) -> dict[str, bool]:
    p = principal(request)
    try:
        return {"updated": _services(request).mcp.ack_inbox(p.user_id, inbox_id, status=payload.status)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1/mcp/handoff/{token}")
def resolve_handoff(token: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).mcp.resolve_handoff(p.user_id, token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Legacy transport retained only for source-level/local compatibility when the
# official MCP SDK is not installed. Production clients must use `/mcp`, which
# is mounted from the official SDK adapter in `mcp_protocol.py`.
# ---------------------------------------------------------------------------
@router.post("/mcp")
async def mcp_transport(request: Request) -> dict[str, Any]:
    services = _services(request)
    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="MCP bearer token required")
    try:
        user_id, scopes, connection_id = services.mcp.authenticate(token)
    except MCPAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON-RPC body") from exc
    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    try:
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28", "2025-11-25"],
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "OVAEL", "version": request.app.version},
                "instructions": "OVAEL exposes scoped learner context and adaptive teaching tools. Never infer access beyond granted scopes.",
                "ttlMs": 300000,
                "cacheScope": "private",
            }
        elif method == "initialize":
            result = {
                "protocolVersion": str(params.get("protocolVersion") or "2025-11-25"),
                "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
                "serverInfo": {"name": "OVAEL", "version": request.app.version},
                "instructions": "OVAEL exposes scoped learner context and adaptive teaching tools. Never infer access beyond granted scopes.",
            }
        elif method == "notifications/initialized":
            result = {}
        elif method == "tools/list":
            result = {"tools": services.mcp.tool_catalog()}
        elif method == "tools/call":
            result_data = services.mcp.call_tool(
                user_id=user_id,
                scopes=scopes,
                name=str(params.get("name") or ""),
                arguments=params.get("arguments") or {},
            )
            result = {
                "content": [{"type": "text", "text": json.dumps(result_data, ensure_ascii=False)}],
                "structuredContent": result_data,
                "isError": False,
            }
        elif method == "resources/list":
            result = {
                "resources": [
                    {"uri": "ovael://learner/summary", "name": "Learner summary", "mimeType": "application/json"}
                ]
            }
        elif method == "resources/read":
            uri = str(params.get("uri") or "")
            if uri == "ovael://learner/summary":
                data = services.mcp.get_context(user_id)
            else:
                raise ValueError("Unsupported resource URI")
            result = {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, ensure_ascii=False)}]}
        else:
            raise ValueError("Unsupported MCP method")
        services.auth.audit(
            user_id,
            "mcp.rpc",
            resource_type="mcp_connection",
            resource_id=connection_id,
            client_type="mcp",
            metadata={"method": method},
        )
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
    except (ValueError, MCPAuthError) as exc:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32000, "message": str(exc)}}

# ---------------------------------------------------------------------------
# Teacher/classroom, course graph and voice companion APIs
# ---------------------------------------------------------------------------

@router.post("/v1/courses/{course_id}/enroll")
def enroll_learner(course_id: str, payload: EnrollmentIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    s = _services(request)
    course = s.db.fetchone("SELECT * FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id))
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    if p.role != "teacher" or course["course_kind"] != "class":
        raise HTTPException(status_code=403, detail={"code": "TEACHER_REQUIRED", "message": "Only the class teacher can enroll learners"})
    learner = s.db.fetchone(
        "SELECT user_id,username,role FROM users WHERE username=? COLLATE NOCASE AND disabled=0",
        (payload.learner_username,),
    )
    if not learner or learner["role"] != "learner":
        raise HTTPException(status_code=404, detail="Learner not found")
    now = datetime.now(timezone.utc).isoformat()
    s.db.execute(
        """INSERT INTO course_enrollments(course_id,learner_user_id,status,enrolled_at)
           VALUES(?,?,'active',?)
           ON CONFLICT(course_id,learner_user_id) DO UPDATE SET status='active'""",
        (course_id, learner["user_id"], now),
    )
    return {"course_id": course_id, "learner_user_id": learner["user_id"], "username": learner["username"], "status": "active"}


@router.delete("/v1/courses/{course_id}/enroll/{learner_user_id}")
def unenroll_learner(course_id: str, learner_user_id: str, request: Request) -> dict[str, bool]:
    p = principal(request); s = _services(request)
    course = s.db.fetchone("SELECT 1 FROM courses WHERE course_id=? AND owner_user_id=? AND course_kind='class'", (course_id, p.user_id))
    if not course or p.role != "teacher":
        raise HTTPException(status_code=403, detail="Only the class teacher can remove learners")
    s.db.execute("DELETE FROM course_enrollments WHERE course_id=? AND learner_user_id=?", (course_id, learner_user_id))
    return {"removed": True}


@router.get("/v1/courses/{course_id}/learners")
def course_learners(course_id: str, request: Request) -> list[dict[str, Any]]:
    p = principal(request); s = _services(request)
    if p.role != "teacher" or not s.db.fetchone("SELECT 1 FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id)):
        raise HTTPException(status_code=403, detail="Teacher course ownership required")
    rows = s.db.fetchall(
        """SELECT u.user_id,u.username,e.status,e.enrolled_at,
                  coalesce(ps.shared_with_teacher,0) AS progress_shared, ps.updated_at AS progress_updated_at
           FROM course_enrollments e JOIN users u ON u.user_id=e.learner_user_id
           LEFT JOIN progress_snapshots ps ON ps.course_id=e.course_id AND ps.learner_user_id=e.learner_user_id
           WHERE e.course_id=? ORDER BY u.username""", (course_id,)
    )
    return [dict(r) for r in rows]


@router.post("/v1/courses/{course_id}/progress-snapshot")
def share_progress_snapshot(course_id: str, payload: ProgressSnapshotIn, request: Request) -> dict[str, Any]:
    p = principal(request); s = _services(request)
    if p.role != "learner":
        raise HTTPException(status_code=403, detail="Only learner accounts publish learner progress")
    enrolled = s.db.fetchone("SELECT 1 FROM course_enrollments WHERE course_id=? AND learner_user_id=? AND status='active'", (course_id, p.user_id))
    if not enrolled:
        raise HTTPException(status_code=403, detail="Learner is not enrolled in this course")
    course = s.db.fetchone("SELECT subject_id FROM courses WHERE course_id=?", (course_id,))
    subject_id = course["subject_id"] if course else None
    valid_concepts = {r["concept_id"] for r in s.db.fetchall("SELECT concept_id FROM concepts WHERE subject_id=?", (subject_id,))} if subject_id else set()
    # Privacy-minimized projection only. Raw answers, transcripts and chat history are forbidden.
    safe_states = []
    for item in payload.concept_states:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("concept_id") or "")[:128]
        status = str(item.get("status") or "unknown")[:64]
        if cid and cid in valid_concepts:
            safe_states.append({"concept_id": cid, "status": status})
    safe_gaps = []
    for item in payload.gap_summary:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("concept_id") or "")[:128]
        state = str(item.get("state") or item.get("status") or "uncertain")[:64]
        if cid and cid in valid_concepts:
            safe_gaps.append({"concept_id": cid, "state": state})
    snapshot = {"concept_states": safe_states[:1000], "gap_summary": safe_gaps[:200]}
    now = datetime.now(timezone.utc).isoformat()
    s.db.execute(
        """INSERT INTO progress_snapshots(course_id,learner_user_id,graph_version,snapshot_json,shared_with_teacher,updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(course_id,learner_user_id) DO UPDATE SET graph_version=excluded.graph_version,
             snapshot_json=excluded.snapshot_json,shared_with_teacher=excluded.shared_with_teacher,updated_at=excluded.updated_at""",
        (course_id, p.user_id, payload.graph_version, s.db.dumps(snapshot), int(payload.shared_with_teacher), now),
    )
    return {"course_id": course_id, "shared_with_teacher": payload.shared_with_teacher, "updated_at": now}


@router.get("/v1/teacher/courses/{course_id}/gaps")
def teacher_gap_summary(course_id: str, request: Request) -> dict[str, Any]:
    p = principal(request); s = _services(request)
    if p.role != "teacher" or not s.db.fetchone("SELECT 1 FROM courses WHERE course_id=? AND owner_user_id=? AND course_kind='class'", (course_id, p.user_id)):
        raise HTTPException(status_code=403, detail="Teacher class ownership required")
    rows = s.db.fetchall(
        "SELECT snapshot_json FROM progress_snapshots WHERE course_id=? AND shared_with_teacher=1", (course_id,)
    )
    status_counts: dict[str, dict[str, int]] = {}
    gap_counts: dict[str, int] = {}
    for row in rows:
        snap = s.db.loads(row["snapshot_json"], {})
        for state in snap.get("concept_states", []) if isinstance(snap, dict) else []:
            cid = str(state.get("concept_id") or "")
            st = str(state.get("status") or "unknown")
            if cid:
                status_counts.setdefault(cid, {})[st] = status_counts.setdefault(cid, {}).get(st, 0) + 1
        for gap in snap.get("gap_summary", []) if isinstance(snap, dict) else []:
            cid = str(gap.get("concept_id") or "")
            if cid:
                gap_counts[cid] = gap_counts.get(cid, 0) + 1
    return {
        "course_id": course_id,
        "learners_sharing": len(rows),
        "concept_status_counts": status_counts,
        "gap_counts": dict(sorted(gap_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "privacy": "aggregate derived progress only; no raw learner conversations",
    }


@router.post("/v1/courses/{course_id}/graph/proposals")
def propose_course_graph(course_id: str, payload: CourseGraphProposalIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).course_graphs.propose(
            user_id=p.user_id, course_id=course_id, document_ids=payload.document_ids, objective=payload.objective
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/v1/courses/{course_id}/graph/proposals")
def list_course_graphs(course_id: str, request: Request) -> list[dict[str, Any]]:
    p = principal(request)
    return _services(request).course_graphs.get(user_id=p.user_id, course_id=course_id)


@router.put("/v1/courses/{course_id}/graph/proposals/{revision_id}")
def edit_course_graph(course_id: str, revision_id: str, payload: CourseGraphEditIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).course_graphs.edit(
            user_id=p.user_id, course_id=course_id, revision_id=revision_id, graph=payload.graph
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1/courses/{course_id}/graph/proposals/{revision_id}/approve")
def approve_course_graph(course_id: str, revision_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).course_graphs.approve(user_id=p.user_id, course_id=course_id, revision_id=revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/v1/courses/{course_id}/agent-spec")
def course_agent_spec(course_id: str, request: Request) -> dict[str, Any]:
    p = principal(request); s = _services(request)
    course = s.db.fetchone(
        """SELECT c.* FROM courses c
           LEFT JOIN course_enrollments e ON e.course_id=c.course_id AND e.learner_user_id=?
           WHERE c.course_id=? AND (c.owner_user_id=? OR e.learner_user_id=?)""",
        (p.user_id, course_id, p.user_id, p.user_id),
    )
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    spec = s.subject_agents.build(subject_id=course["subject_id"], course_id=course_id)
    return {
        "subject_id": spec.subject_id,
        "subject_name": spec.subject_name,
        "course_id": spec.course_id,
        "course_name": spec.course_name,
        "evaluator_families": list(spec.evaluator_families),
        "source_policy": spec.source_policy,
        "tools": list(spec.tools),
        "dynamic": spec.dynamic,
        "note": "System prompt is compiled server-side; hidden reasoning is never exposed.",
    }


@router.post("/v1/voice/calls")
def start_voice_call(payload: VoiceCallStartIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).voice_calls.start(
            user_id=p.user_id, teaching_session_id=payload.teaching_session_id,
            playback_rate=payload.playback_rate, language=payload.language, voice=payload.voice,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/v1/voice/calls/{call_id}/speed")
def update_voice_speed(call_id: str, request: Request, playback_rate: float = Query(..., ge=0.75, le=3.0)) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).voice_calls.set_playback_rate(user_id=p.user_id, call_id=call_id, playback_rate=playback_rate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1/voice/calls/{call_id}/turn")
async def voice_call_turn(
    call_id: str,
    request: Request,
    audio: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    p = principal(request)
    if not idempotency_key:
        raise HTTPException(status_code=400, detail={"code": "IDEMPOTENCY_REQUIRED", "message": "Idempotency-Key is required"})
    data = await audio.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio exceeds voice-turn limit")
    try:
        return _services(request).voice_calls.turn(
            user_id=p.user_id, call_id=call_id, audio=data,
            filename=audio.filename or "audio.wav", idempotency_key=idempotency_key,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1/voice/calls/{call_id}/end")
def end_voice_call(call_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    try:
        return _services(request).voice_calls.end(user_id=p.user_id, call_id=call_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/v1/courses/{course_id}/graph")
def active_course_graph(course_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    from .documents import _active_course_graph
    try:
        return _active_course_graph(_services(request).db, viewer_user_id=p.user_id, course_id=course_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
