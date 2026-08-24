from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .auth import AuthService
from .database import Database
from .memory import MemoryService
from .retrieval import RetrievalService
from .teaching import LearningSessionStart, LearningTurnInput, TeachingService


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


DEFAULT_SCOPES = {
    "learning.context.read",
    "learning.map.read",
    "learning.materials.search",
    "learning.session.create",
    "learning.session.interact",
    "learning.suggestions.submit",
    "learning.external_events.submit",
}


class MCPAuthError(ValueError):
    pass


class MCPService:
    """OVAEL's model-facing capability boundary.

    This service implements tool semantics independently of transport.
    `mcp_protocol.py` mounts the official MCP SDK transport when the dependency is
    installed. No tool gets raw SQL or unrestricted learner-memory access.
    """

    def __init__(
        self,
        db: Database,
        auth: AuthService,
        memory: MemoryService,
        retrieval: RetrievalService,
        teaching: TeachingService,
        *,
        context_ttl_seconds: int,
        public_base_url: str,
    ):
        self.db = db
        self.auth = auth
        self.memory = memory
        self.retrieval = retrieval
        self.teaching = teaching
        self.context_ttl_seconds = context_ttl_seconds
        self.public_base_url = public_base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Connection tokens / scopes
    # ------------------------------------------------------------------
    def create_connection(
        self, *, user_id: str, client_name: str, scopes: list[str] | None = None, days: int = 30
    ) -> dict[str, Any]:
        requested = set(scopes or sorted(DEFAULT_SCOPES))
        if not requested or not requested <= DEFAULT_SCOPES:
            raise MCPAuthError("One or more MCP scopes are not allowed")
        token = "ovmcp_" + secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        connection_id = str(uuid4())
        now = _now()
        exp = now + timedelta(days=max(1, min(90, days)))
        self.db.execute(
            """
            INSERT INTO mcp_connections(connection_id,user_id,client_name,scopes_json,token_hash,created_at,expires_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                connection_id,
                user_id,
                client_name[:100],
                self.db.dumps(sorted(requested)),
                token_hash,
                _iso(now),
                _iso(exp),
            ),
        )
        self.auth.audit(
            user_id,
            "mcp.connection.create",
            resource_type="mcp_connection",
            resource_id=connection_id,
            client_type=client_name[:100],
            metadata={"scopes": sorted(requested)},
        )
        return {
            "connection_id": connection_id,
            "client_name": client_name,
            "scopes": sorted(requested),
            "access_token": token,
            "expires_at": _iso(exp),
            "mcp_url": f"{self.public_base_url}/mcp",
        }

    def authenticate(self, token: str) -> tuple[str, set[str], str]:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.fetchone(
            "SELECT * FROM mcp_connections WHERE token_hash=?",
            (token_hash,),
        )
        if not row or row["revoked_at"]:
            raise MCPAuthError("Invalid MCP access token")
        if datetime.fromisoformat(row["expires_at"]) <= _now():
            raise MCPAuthError("MCP access token expired")
        return row["user_id"], set(self.db.loads(row["scopes_json"], [])), row["connection_id"]

    def list_connections(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT connection_id,client_name,scopes_json,created_at,expires_at,revoked_at FROM mcp_connections WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        )
        return [
            {
                "connection_id": r["connection_id"],
                "client_name": r["client_name"],
                "scopes": self.db.loads(r["scopes_json"], []),
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
                "revoked": bool(r["revoked_at"]),
            }
            for r in rows
        ]

    def revoke_connection(self, user_id: str, connection_id: str) -> bool:
        count = self.db.execute(
            "UPDATE mcp_connections SET revoked_at=? WHERE connection_id=? AND user_id=? AND revoked_at IS NULL",
            (_iso(_now()), connection_id, user_id),
        )
        if count:
            self.auth.audit(
                user_id,
                "mcp.connection.revoke",
                resource_type="mcp_connection",
                resource_id=connection_id,
            )
        return bool(count)

    # ------------------------------------------------------------------
    # Browser-local context relay
    # ------------------------------------------------------------------
    def put_context(self, user_id: str, capsule: dict[str, Any]) -> dict[str, Any]:
        if not self.memory.personalization_enabled(user_id):
            self.db.execute("DELETE FROM mcp_context_relay WHERE user_id=?", (user_id,))
            return {"status": "personalization_paused", "expires_at": None}
        raw = json.dumps(capsule, ensure_ascii=False)
        if len(raw.encode()) > 32_000:
            raise ValueError("Context capsule is too large")
        forbidden = {"password", "api_key", "access_token", "refresh_token", "raw_event_ledger", "secret", "token"}
        def scan(value: Any, depth: int = 0) -> None:
            if depth > 8:
                raise ValueError("Context capsule is nested too deeply")
            if isinstance(value, dict):
                for key, child in value.items():
                    lowered = str(key).strip().lower()
                    if lowered in forbidden or lowered.endswith("_password") or lowered.endswith("_secret") or lowered.endswith("_token"):
                        raise ValueError("Context capsule contains a forbidden field")
                    scan(child, depth + 1)
            elif isinstance(value, list):
                for child in value[:500]:
                    scan(child, depth + 1)
        scan(capsule)
        now = _now()
        exp = now + timedelta(seconds=self.context_ttl_seconds)
        self.db.execute(
            """
            INSERT INTO mcp_context_relay(user_id,capsule_json,generated_at,expires_at,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET capsule_json=excluded.capsule_json,generated_at=excluded.generated_at,expires_at=excluded.expires_at,updated_at=excluded.updated_at
            """,
            (user_id, raw, _iso(now), _iso(exp), _iso(now)),
        )
        return {"status": "synced", "expires_at": _iso(exp)}

    def get_context(self, user_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM mcp_context_relay WHERE user_id=?", (user_id,))
        if not row or datetime.fromisoformat(row["expires_at"]) <= _now():
            raise ValueError("context_stale")
        capsule = self.db.loads(row["capsule_json"], {})
        return {
            "capsule": capsule,
            "generated_at": row["generated_at"],
            "expires_at": row["expires_at"],
        }

    # ------------------------------------------------------------------
    # Tool surface
    # ------------------------------------------------------------------
    @staticmethod
    def tool_catalog() -> list[dict[str, Any]]:
        return [
            _tool("get_learning_context", "Read the learner's bounded active OVAEL context capsule.", {"type": "object", "properties": {}}),
            _tool(
                "search_learning_material",
                "Search authorized trusted learning material for a subject/concept.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "subject_id": {"type": "string"},
                        "course_id": {"type": ["string", "null"]},
                        "concept_ids": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query", "subject_id"],
                },
            ),
            _tool(
                "get_learning_map",
                "Read a compact learning-map projection, optionally scoped to a subject.",
                {"type": "object", "properties": {"subject_id": {"type": ["string", "null"]}}},
            ),
            _tool(
                "begin_teaching_session",
                "Start an OVAEL adaptive teaching session.",
                {
                    "type": "object",
                    "properties": {
                        "subject_id": {"type": "string"},
                        "concept_id": {"type": "string"},
                        "course_id": {"type": ["string", "null"]},
                        "goal": {"type": ["string", "null"]},
                        "mode": {"type": "string", "enum": ["study", "practice", "review", "calibration"]},
                    },
                    "required": ["subject_id", "concept_id"],
                },
            ),
            _tool(
                "next_teaching_turn",
                "Submit the learner's latest response and obtain OVAEL's next dynamic TeachingMove.",
                {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "response": {"type": "string"},
                        "selected_option": {"type": ["integer", "null"]},
                        "numeric_value": {"type": ["number", "null"]},
                        "code": {"type": ["string", "null"]},
                        "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                        "hint_count": {"type": "integer", "minimum": 0, "maximum": 100},
                        "attempt_count": {"type": "integer", "minimum": 1, "maximum": 100},
                        "response_seconds": {"type": ["number", "null"], "minimum": 0, "maximum": 86400},
                        "idempotency_key": {"type": "string"},
                    },
                    "required": ["session_id", "idempotency_key"],
                },
            ),
            _tool(
                "submit_teaching_suggestion",
                "Submit an advisory teaching strategy for OVAEL's X/Carl agents to consider.",
                {
                    "type": "object",
                    "properties": {"session_id": {"type": "string"}, "suggestion": {"type": "string"}},
                    "required": ["session_id", "suggestion"],
                },
            ),
            _tool(
                "handoff_to_ovael",
                "Create a short-lived deep link that continues the external teaching session in the OVAEL web app.",
                {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
            ),
            _tool(
                "complete_external_session",
                "Complete an external teaching session and enqueue structured evidence for OVAEL import.",
                {
                    "type": "object",
                    "properties": {"session_id": {"type": "string"}, "summary": {"type": "string"}, "idempotency_key": {"type": "string"}},
                    "required": ["session_id", "idempotency_key"],
                },
            ),
        ]

    def call_tool(self, *, user_id: str, scopes: set[str], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        required = {
            "get_learning_context": "learning.context.read",
            "search_learning_material": "learning.materials.search",
            "get_learning_map": "learning.map.read",
            "begin_teaching_session": "learning.session.create",
            "next_teaching_turn": "learning.session.interact",
            "submit_teaching_suggestion": "learning.suggestions.submit",
            "handoff_to_ovael": "learning.session.interact",
            "complete_external_session": "learning.external_events.submit",
        }.get(name)
        if not required:
            raise ValueError("Unknown MCP tool")
        if required not in scopes:
            raise MCPAuthError(f"Missing required scope: {required}")

        if name == "get_learning_context":
            return self.get_context(user_id)
        if name == "search_learning_material":
            hits = self.retrieval.search(
                query=str(arguments.get("query") or ""),
                subject_id=str(arguments.get("subject_id") or ""),
                concept_ids=list(arguments.get("concept_ids") or []),
                limit=max(1, min(10, int(arguments.get("limit") or 6))),
                verified_only=True,
                user_id=user_id,
                course_id=arguments.get("course_id"),
            )
            return {"hits": [h.model_dump(mode="json") for h in hits]}
        if name == "get_learning_map":
            subject_id = arguments.get("subject_id")
            if self.teaching.memory_mode == "local_first":
                relay = self.get_context(user_id).get("capsule", {})
                raw_states = relay.get("concept_states") or relay.get("states") or []
                if isinstance(raw_states, dict):
                    raw_states = [dict({"concept_id": key}, **(value if isinstance(value, dict) else {})) for key, value in raw_states.items()]
                nodes: list[dict[str, Any]] = []
                concept_ids: list[str] = []
                for item in raw_states[:250] if isinstance(raw_states, list) else []:
                    if not isinstance(item, dict):
                        continue
                    cid = str(item.get("concept_id") or "").strip()
                    if not cid:
                        continue
                    concept = self.db.fetchone("SELECT name,subject_id FROM concepts WHERE concept_id=?", (cid,))
                    if not concept or (subject_id and concept["subject_id"] != subject_id):
                        continue
                    concept_ids.append(cid)
                    nodes.append({
                        "concept_id": cid,
                        "name": concept["name"],
                        "subject_id": concept["subject_id"],
                        "status": item.get("status", "unknown"),
                        "confidence": item.get("confidence"),
                    })
                edges: list[dict[str, Any]] = []
                if concept_ids:
                    placeholders = ",".join("?" for _ in concept_ids)
                    rows = self.db.fetchall(
                        f"""SELECT source_concept_id,target_concept_id,relation,strength FROM concept_edges
                        WHERE source_concept_id IN ({placeholders}) AND target_concept_id IN ({placeholders})
                        LIMIT 500""",
                        tuple(concept_ids + concept_ids),
                    )
                    edges = [dict(r) for r in rows]
                return {"nodes": nodes, "edges": edges, "truncated": len(nodes) >= 250 or len(edges) >= 500, "source": "context_relay"}
            graph = self.memory.learning_map(user_id, subject_id)
            return {
                "nodes": [n.model_dump(mode="json") for n in graph.nodes[:250]],
                "edges": [e.model_dump(mode="json") for e in graph.edges[:500]],
                "truncated": len(graph.nodes) > 250 or len(graph.edges) > 500,
                "source": "server_memory",
            }
        if name == "begin_teaching_session":
            req = LearningSessionStart(
                subject_id=arguments["subject_id"],
                concept_id=arguments["concept_id"],
                course_id=arguments.get("course_id"),
                goal=arguments.get("goal"),
                mode=arguments.get("mode", "study"),
                source_client="mcp",
                context_capsule=self.get_context(user_id).get("capsule", {}),
            )
            return self.teaching.start(user_id=user_id, request=req)
        if name == "next_teaching_turn":
            turn = LearningTurnInput(
                response=str(arguments.get("response") or ""),
                selected_option=arguments.get("selected_option"),
                numeric_value=arguments.get("numeric_value"),
                code=arguments.get("code"),
                confidence=arguments.get("confidence"),
                hint_count=int(arguments.get("hint_count") or 0),
                attempt_count=int(arguments.get("attempt_count") or 1),
                response_seconds=arguments.get("response_seconds"),
            )
            result = self.teaching.turn(
                user_id=user_id,
                session_id=str(arguments["session_id"]),
                turn=turn,
                idempotency_key=str(arguments["idempotency_key"]),
            )
            delta = result.get("state_delta")
            if self.teaching.memory_mode == "local_first" and isinstance(delta, dict):
                try:
                    current = self.get_context(user_id).get("capsule", {})
                except ValueError:
                    current = {}
                merged = dict(current)
                # The relay is a bounded active-context projection, not a transport
                # for the full state/event delta. Keep only the most relevant
                # compact state and let the External Learning Inbox carry events.
                merged["concept_states"] = list(delta.get("concept_states", []))[:50]
                merged["adaptive_states"] = list(delta.get("adaptive_states", []))[:50]
                merged["gap_hypotheses"] = list(delta.get("gap_hypotheses", []))[:30]
                merged["subject_id"] = delta.get("subject_id")
                try:
                    self.put_context(user_id, merged)
                except ValueError:
                    # A successful teaching turn must never be rolled back at the
                    # API boundary merely because the optional relay projection
                    # grew too large. The client can resync a smaller capsule.
                    pass
            return result
        if name == "submit_teaching_suggestion":
            suggestion = str(arguments.get("suggestion") or "").strip()
            if not suggestion or len(suggestion) > 4000:
                raise ValueError("Suggestion is empty or too long")
            session = self.teaching.get(user_id=user_id, session_id=str(arguments["session_id"]))
            if session["status"] != "active":
                raise ValueError("Teaching session is not active")
            suggestion_id = str(uuid4())
            self.db.execute(
                "INSERT INTO teaching_suggestions(suggestion_id,teaching_session_id,owner_user_id,suggestion,status,created_at) VALUES(?,?,?,?,'pending',?)",
                (suggestion_id, session["teaching_session_id"], user_id, suggestion, _iso(_now())),
            )
            self.auth.audit(
                user_id,
                "mcp.teaching_suggestion",
                resource_type="teaching_session",
                resource_id=session["teaching_session_id"],
                client_type="mcp",
                metadata={"suggestion_id": suggestion_id},
            )
            return {
                "status": "accepted_as_advisory",
                "suggestion_id": suggestion_id,
                "note": "The next OVAEL teaching plan may use this as untrusted advisory context; it cannot directly change mastery.",
            }
        if name == "handoff_to_ovael":
            return self.create_handoff(user_id, str(arguments["session_id"]))
        if name == "complete_external_session":
            return self.complete_external(
                user_id=user_id,
                session_id=str(arguments["session_id"]),
                summary=str(arguments.get("summary") or "")[:8000],
                idempotency_key=str(arguments["idempotency_key"]),
            )
        raise ValueError("Unknown MCP tool")

    # ------------------------------------------------------------------
    def create_handoff(self, user_id: str, session_id: str) -> dict[str, Any]:
        self.teaching.get(user_id=user_id, session_id=session_id)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = _now()
        exp = now + timedelta(minutes=10)
        self.db.execute(
            "INSERT INTO handoff_tokens(token_hash,user_id,teaching_session_id,created_at,expires_at) VALUES(?,?,?,?,?)",
            (token_hash, user_id, session_id, _iso(now), _iso(exp)),
        )
        return {
            "url": f"{self.public_base_url}/app/continue/{token}",
            "expires_at": _iso(exp),
        }

    def resolve_handoff(self, user_id: str, token: str) -> dict[str, Any]:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.fetchone("SELECT * FROM handoff_tokens WHERE token_hash=?", (token_hash,))
        if not row or row["user_id"] != user_id or row["consumed_at"]:
            raise ValueError("Invalid handoff token")
        if datetime.fromisoformat(row["expires_at"]) <= _now():
            raise ValueError("Handoff token expired")
        self.db.execute("UPDATE handoff_tokens SET consumed_at=? WHERE token_hash=?", (_iso(_now()), token_hash))
        return self.teaching.get(user_id=user_id, session_id=row["teaching_session_id"])

    def complete_external(self, *, user_id: str, session_id: str, summary: str, idempotency_key: str) -> dict[str, Any]:
        session = self.teaching.get(user_id=user_id, session_id=session_id)
        row = self.db.fetchone(
            "SELECT inbox_id,status FROM external_learning_inbox WHERE user_id=? AND external_session_id=? AND idempotency_key=?",
            (user_id, session_id, idempotency_key),
        )
        if row:
            return {"inbox_id": row["inbox_id"], "status": row["status"], "idempotent_replay": True}
        completed = self.teaching.complete(user_id=user_id, session_id=session_id)
        state_delta = completed.get("state_delta") or session.get("state_delta")
        inbox_id = str(uuid4())
        now = _iso(_now())
        evidence = {
            "teaching_session_id": session_id,
            "subject_id": session["subject_id"],
            "concept_id": session["concept_id"],
            "turn_count": session["turn_count"],
            "summary": summary[:8000],
            "state_delta": state_delta,
            "source": "mcp_external",
        }
        try:
            self.db.execute(
                """
                INSERT INTO external_learning_inbox(inbox_id,user_id,external_session_id,concept_id,idempotency_key,evidence_json,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'pending',?,?)
                """,
                (inbox_id, user_id, session_id, session["concept_id"], idempotency_key, self.db.dumps(evidence), now, now),
            )
        except Exception:
            replay = self.db.fetchone(
                "SELECT inbox_id,status FROM external_learning_inbox WHERE user_id=? AND external_session_id=? AND idempotency_key=?",
                (user_id, session_id, idempotency_key),
            )
            if replay:
                return {"inbox_id": replay["inbox_id"], "status": replay["status"], "idempotent_replay": True}
            raise
        return {"inbox_id": inbox_id, "status": "pending", "idempotent_replay": False, "state_delta": state_delta}

    def list_inbox(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM external_learning_inbox WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
            (user_id,),
        )
        return [
            {
                "inbox_id": r["inbox_id"],
                "external_session_id": r["external_session_id"],
                "concept_id": r["concept_id"],
                "evidence": self.db.loads(r["evidence_json"], {}),
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def ack_inbox(self, user_id: str, inbox_id: str, *, status: str) -> bool:
        if status not in {"accepted", "rejected"}:
            raise ValueError("Status must be accepted or rejected")
        count = self.db.execute(
            "UPDATE external_learning_inbox SET status=?,updated_at=? WHERE inbox_id=? AND user_id=? AND status='pending'",
            (status, _iso(_now()), inbox_id, user_id),
        )
        return bool(count)


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {"readOnlyHint": name.startswith("get_") or name.startswith("search_")},
    }
