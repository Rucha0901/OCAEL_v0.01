from __future__ import annotations

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
    description: str | None = Field(default=None, max_length=4000)


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


@router.get("/v1/health")
def v1_health(request: Request) -> dict[str, Any]:
    s = _services(request)
    return {
        "status": "ok",
        "product": "OVAEL",
        "version": request.app.version,
        "database": "sqlite-local",
        "mimo_configured": s.models.mimo_available,
        "mcp": "official-sdk" if getattr(request.app.state, "mcp_protocol_available", False) else "legacy-fallback-only",
        "dynamic_teaching": "enabled",
        "agents": ["X", "Sam", "Carl", "Trav"],
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@router.post("/v1/auth/register")
def register(payload: RegisterIn, request: Request, response: Response) -> dict[str, Any]:
    try:
        result = _services(request).auth.register(
            payload.username, payload.password, user_agent=request.headers.get("user-agent")
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
    return {"user_id": p.user_id, "username": p.username}


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
    principal(request)
    return _services(request).subjects.list_subjects()


@router.post("/v1/courses")
def create_course(payload: CourseIn, request: Request) -> dict[str, Any]:
    p = principal(request)
    s = _services(request)
    if payload.subject_id:
        if not s.db.fetchone("SELECT 1 FROM subjects WHERE subject_id=?", (payload.subject_id,)):
            raise HTTPException(status_code=400, detail="Unknown subject")
    course_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    s.db.execute(
        "INSERT INTO courses(course_id,owner_user_id,subject_id,name,description,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (course_id, p.user_id, payload.subject_id, payload.name, payload.description, now, now),
    )
    return {"course_id": course_id, **payload.model_dump(), "visibility": "private", "created_at": now}


@router.get("/v1/courses")
def list_courses(request: Request) -> list[dict[str, Any]]:
    p = principal(request)
    rows = _services(request).db.fetchall(
        "SELECT * FROM courses WHERE owner_user_id=? ORDER BY updated_at DESC", (p.user_id,)
    )
    return [dict(r) for r in rows]


@router.get("/v1/courses/{course_id}")
def get_course(course_id: str, request: Request) -> dict[str, Any]:
    p = principal(request)
    row = _services(request).db.fetchone(
        "SELECT * FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id)
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
    s.db.execute("DELETE FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id))
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Documents/jobs
# ---------------------------------------------------------------------------
@router.post("/v1/documents/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    course_id: str | None = Query(default=None),
) -> dict[str, Any]:
    p = principal(request)
    s = _services(request)
    if course_id and not s.db.fetchone(
        "SELECT 1 FROM courses WHERE course_id=? AND owner_user_id=?", (course_id, p.user_id)
    ):
        raise HTTPException(status_code=404, detail="Course not found")
    try:
        return await s.documents.upload(user_id=p.user_id, upload=file, course_id=course_id)
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
        "SELECT 1 FROM courses WHERE course_id=? AND owner_user_id=?", (payload.course_id, p.user_id)
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
        ],
        "sub_agents": {
            "max_per_lead": 3,
            "max_total_per_turn": _services(request).settings.agent_max_total_specialists,
            "recursive_spawn": False,
            "execution": "parallel_when_independent",
        },
    }


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
