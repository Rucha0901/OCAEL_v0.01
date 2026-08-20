from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import Settings
from .database import Database


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(slots=True, frozen=True)
class AuthPrincipal:
    user_id: str
    username: str
    session_token_id: str


class AuthError(ValueError):
    pass


class AuthService:
    """First-party OVAEL authentication.

    Passwords and recovery keys are Argon2id-hashed. Access tokens are signed JWTs
    whose JTI is also persisted so logout/revocation takes effect immediately.
    The class deliberately owns all credential operations in one subsystem.
    """

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
        # Never fall back to a predictable signing secret. If no secret is
        # configured, use a process-local random key. This keeps local development
        # usable while invalidating sessions on restart instead of allowing forged
        # JWTs from a known default. Deployed auth-required mode still requires an
        # explicit OVAEL_AUTH_SECRET at app startup.
        self._secret = settings.auth_secret or secrets.token_urlsafe(48)

    @staticmethod
    def normalize_username(username: str) -> str:
        value = username.strip()
        if not 3 <= len(value) <= 64:
            raise AuthError("Learner ID must be between 3 and 64 characters")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        if any(ch not in allowed for ch in value):
            raise AuthError("Learner ID may contain letters, numbers, '.', '_' and '-' only")
        return value

    @staticmethod
    def validate_password(password: str) -> None:
        if len(password) < 10:
            raise AuthError("Password must contain at least 10 characters")
        if len(password) > 256:
            raise AuthError("Password is too long")
        # Do not impose brittle composition rules. Length plus Argon2id and rate
        # limiting is safer and more usable than mandatory symbol/case patterns.

    def _rate_key(self, value: str) -> str:
        return hashlib.sha256(value.strip().casefold().encode("utf-8")).hexdigest()

    def _check_rate_limit(self, action: str, value: str, *, limit: int = 8, window_minutes: int = 15) -> None:
        now = utc_now()
        key_hash = self._rate_key(value)
        row = self.db.fetchone(
            "SELECT * FROM auth_rate_limits WHERE action=? AND key_hash=?",
            (action, key_hash),
        )
        if not row:
            return
        blocked_until = datetime.fromisoformat(row["blocked_until"]) if row["blocked_until"] else None
        if blocked_until and blocked_until > now:
            raise AuthError("Too many attempts. Try again later")
        started = datetime.fromisoformat(row["window_started_at"])
        if started + timedelta(minutes=window_minutes) <= now:
            self.db.execute(
                "DELETE FROM auth_rate_limits WHERE action=? AND key_hash=?",
                (action, key_hash),
            )
            return
        if int(row["attempts"]) >= limit:
            until = now + timedelta(minutes=window_minutes)
            self.db.execute(
                "UPDATE auth_rate_limits SET blocked_until=?,updated_at=? WHERE action=? AND key_hash=?",
                (iso(until), iso(now), action, key_hash),
            )
            raise AuthError("Too many attempts. Try again later")

    def _record_failed_attempt(self, action: str, value: str, *, limit: int = 8, window_minutes: int = 15) -> None:
        now = utc_now()
        key_hash = self._rate_key(value)
        row = self.db.fetchone(
            "SELECT * FROM auth_rate_limits WHERE action=? AND key_hash=?",
            (action, key_hash),
        )
        if not row or datetime.fromisoformat(row["window_started_at"]) + timedelta(minutes=window_minutes) <= now:
            self.db.execute(
                """
                INSERT INTO auth_rate_limits(action,key_hash,window_started_at,attempts,blocked_until,updated_at)
                VALUES(?,?,?,?,NULL,?)
                ON CONFLICT(action,key_hash) DO UPDATE SET
                  window_started_at=excluded.window_started_at,attempts=excluded.attempts,
                  blocked_until=NULL,updated_at=excluded.updated_at
                """,
                (action, key_hash, iso(now), 1, iso(now)),
            )
            return
        attempts = int(row["attempts"]) + 1
        blocked_until = iso(now + timedelta(minutes=window_minutes)) if attempts >= limit else None
        self.db.execute(
            "UPDATE auth_rate_limits SET attempts=?,blocked_until=?,updated_at=? WHERE action=? AND key_hash=?",
            (attempts, blocked_until, iso(now), action, key_hash),
        )

    def _clear_rate_limit(self, action: str, value: str) -> None:
        self.db.execute(
            "DELETE FROM auth_rate_limits WHERE action=? AND key_hash=?",
            (action, self._rate_key(value)),
        )

    def register(self, username: str, password: str, *, user_agent: str | None = None) -> dict[str, Any]:
        username = self.normalize_username(username)
        self.validate_password(password)
        if self.db.fetchone("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)):
            raise AuthError("Learner ID is already registered")
        user_id = str(uuid4())
        recovery_key = self._new_recovery_key()
        now = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users(user_id,username,password_hash,recovery_hash,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    user_id,
                    username,
                    self._ph.hash(password),
                    self._ph.hash(recovery_key),
                    iso(now),
                    iso(now),
                ),
            )
        token = self._issue_session(user_id, username, user_agent=user_agent)
        self.audit(user_id, "auth.register")
        return {
            "user_id": user_id,
            "username": username,
            **token,
            "recovery_key": recovery_key,
        }

    def login(self, username: str, password: str, *, user_agent: str | None = None) -> dict[str, Any]:
        username = self.normalize_username(username)
        self._check_rate_limit("login", username)
        row = self.db.fetchone("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,))
        if not row or row["disabled"]:
            self._record_failed_attempt("login", username)
            raise AuthError("Invalid learner ID or password")
        try:
            ok = self._ph.verify(row["password_hash"], password)
        except (VerifyMismatchError, InvalidHashError):
            ok = False
        if not ok:
            self._record_failed_attempt("login", username)
            raise AuthError("Invalid learner ID or password")
        self._clear_rate_limit("login", username)
        if self._ph.check_needs_rehash(row["password_hash"]):
            self.db.execute(
                "UPDATE users SET password_hash=?,updated_at=? WHERE user_id=?",
                (self._ph.hash(password), iso(utc_now()), row["user_id"]),
            )
        token = self._issue_session(row["user_id"], row["username"], user_agent=user_agent)
        self.audit(row["user_id"], "auth.login")
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            **token,
        }

    def authenticate_token(self, token: str) -> AuthPrincipal:
        try:
            payload = jwt.decode(token, self._secret, algorithms=["HS256"], audience="ovael-web")
        except jwt.PyJWTError as exc:
            raise AuthError("Invalid or expired session") from exc
        user_id = str(payload.get("sub") or "")
        jti = str(payload.get("jti") or "")
        if not user_id or not jti:
            raise AuthError("Invalid session")
        row = self.db.fetchone(
            """
            SELECT s.*,u.username,u.disabled FROM auth_sessions s
            JOIN users u ON u.user_id=s.user_id
            WHERE s.session_token_id=? AND s.user_id=?
            """,
            (jti, user_id),
        )
        if not row or row["revoked_at"] or row["disabled"]:
            raise AuthError("Session has been revoked")
        if datetime.fromisoformat(row["expires_at"]) <= utc_now():
            raise AuthError("Session has expired")
        return AuthPrincipal(user_id=user_id, username=row["username"], session_token_id=jti)

    def refresh(self, refresh_token: str) -> dict[str, str]:
        token = (refresh_token or "").strip()
        if not token:
            raise AuthError("Refresh token is required")
        token_hash = self.hash_token(token)
        row = self.db.fetchone(
            """SELECT s.*,u.username,u.disabled FROM auth_sessions s
            JOIN users u ON u.user_id=s.user_id
            WHERE s.refresh_token_hash=?""",
            (token_hash,),
        )
        if not row or row["revoked_at"] or row["disabled"]:
            raise AuthError("Invalid refresh session")
        refresh_exp_raw = row["refresh_expires_at"] or row["expires_at"]
        refresh_exp = datetime.fromisoformat(refresh_exp_raw)
        if refresh_exp <= utc_now():
            raise AuthError("Refresh session has expired")

        # Rotate the opaque refresh credential on every use. Reuse of an older
        # token therefore fails immediately without exposing JWT signing state.
        replacement = "ovrefresh_" + secrets.token_urlsafe(48)
        now = utc_now()
        access_exp = now + timedelta(minutes=self.settings.access_token_minutes)
        payload = {
            "sub": row["user_id"],
            "username": row["username"],
            "jti": row["session_token_id"],
            "iat": int(now.timestamp()),
            "exp": int(access_exp.timestamp()),
            "aud": "ovael-web",
            "iss": "ovael",
        }
        access_token = jwt.encode(payload, self._secret, algorithm="HS256")
        rotated = self.db.execute(
            """UPDATE auth_sessions SET refresh_token_hash=?
            WHERE session_token_id=? AND refresh_token_hash=? AND revoked_at IS NULL""",
            (self.hash_token(replacement), row["session_token_id"], token_hash),
        )
        if rotated != 1:
            raise AuthError("Refresh token has already been rotated")
        self.audit(row["user_id"], "auth.refresh")
        return {
            "access_token": access_token,
            "expires_at": iso(access_exp),
            "refresh_token": replacement,
            "refresh_expires_at": iso(refresh_exp),
            "token_type": "bearer",
        }

    def logout(self, principal: AuthPrincipal) -> None:
        self.db.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE session_token_id=? AND revoked_at IS NULL",
            (iso(utc_now()), principal.session_token_id),
        )
        self.audit(principal.user_id, "auth.logout")

    def logout_all(self, principal: AuthPrincipal) -> int:
        count = self.db.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (iso(utc_now()), principal.user_id),
        )
        self.audit(principal.user_id, "auth.logout_all", metadata={"count": count})
        return count

    def change_password(self, principal: AuthPrincipal, current_password: str, new_password: str) -> None:
        self.validate_password(new_password)
        row = self.db.fetchone("SELECT password_hash FROM users WHERE user_id=?", (principal.user_id,))
        if not row:
            raise AuthError("Unknown account")
        try:
            if not self._ph.verify(row["password_hash"], current_password):
                raise AuthError("Current password is incorrect")
        except (VerifyMismatchError, InvalidHashError) as exc:
            raise AuthError("Current password is incorrect") from exc
        now = iso(utc_now())
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?,updated_at=? WHERE user_id=?",
                (self._ph.hash(new_password), now, principal.user_id),
            )
            conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND session_token_id<>? AND revoked_at IS NULL",
                (now, principal.user_id, principal.session_token_id),
            )
        self.audit(principal.user_id, "auth.change_password")

    def recover(self, username: str, recovery_key: str, new_password: str) -> dict[str, str]:
        username = self.normalize_username(username)
        self.validate_password(new_password)
        self._check_rate_limit("recover", username, limit=6, window_minutes=30)
        row = self.db.fetchone("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,))
        if not row or row["disabled"]:
            self._record_failed_attempt("recover", username, limit=6, window_minutes=30)
            raise AuthError("Invalid recovery credentials")
        try:
            valid = self._ph.verify(row["recovery_hash"], recovery_key.strip())
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not valid:
            self._record_failed_attempt("recover", username, limit=6, window_minutes=30)
            raise AuthError("Invalid recovery credentials")
        self._clear_rate_limit("recover", username)
        replacement = self._new_recovery_key()
        now = iso(utc_now())
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?,recovery_hash=?,updated_at=? WHERE user_id=?",
                (self._ph.hash(new_password), self._ph.hash(replacement), now, row["user_id"]),
            )
            conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (now, row["user_id"]),
            )
        self.audit(row["user_id"], "auth.recover")
        return {"user_id": row["user_id"], "recovery_key": replacement}

    def verify_password(self, principal: AuthPrincipal, password: str) -> None:
        row = self.db.fetchone("SELECT password_hash FROM users WHERE user_id=?", (principal.user_id,))
        if not row:
            raise AuthError("Unknown account")
        try:
            valid = self._ph.verify(row["password_hash"], password)
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not valid:
            raise AuthError("Password is incorrect")

    def delete_account(self, principal: AuthPrincipal, password: str) -> list[str]:
        """Delete account-owned backend data and return object keys for cleanup."""
        self.verify_password(principal, password)
        user_id = principal.user_id
        username_hash = self._rate_key(principal.username)
        rows = self.db.fetchall("SELECT storage_key FROM documents WHERE owner_user_id=? AND storage_key IS NOT NULL", (user_id,))
        storage_keys = [str(r["storage_key"]) for r in rows if r["storage_key"]]
        shadow_rows = self.db.fetchall("SELECT state_json FROM teaching_sessions WHERE owner_user_id=?", (user_id,))
        shadow_user_ids: set[str] = set()
        for shadow_row in shadow_rows:
            state = self.db.loads(shadow_row["state_json"], {})
            engine_id = str(state.get("engine_user_id") or "")
            if engine_id.startswith(f"lf:{user_id}:"):
                shadow_user_ids.add(engine_id)
        with self.db.transaction() as conn:
            # Learning/event data. Session deletion cascades events, interventions
            # and summaries; remaining derived/compacted tables are explicit.
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM concept_state WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM adaptive_state WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM gap_hypotheses WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM compacted_history WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM privacy_settings WHERE user_id=?", (user_id,))
            for shadow_id in shadow_user_ids:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM concept_state WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM adaptive_state WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM gap_hypotheses WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM compacted_history WHERE user_id=?", (shadow_id,))
                conn.execute("DELETE FROM privacy_settings WHERE user_id=?", (shadow_id,))

            # Product/content data and derived private retrieval chunks.
            conn.execute("DELETE FROM knowledge_chunks WHERE owner_user_id=?", (user_id,))
            conn.execute("DELETE FROM jobs WHERE owner_user_id=?", (user_id,))
            conn.execute("DELETE FROM documents WHERE owner_user_id=?", (user_id,))
            conn.execute("DELETE FROM courses WHERE owner_user_id=?", (user_id,))
            conn.execute("DELETE FROM teaching_sessions WHERE owner_user_id=?", (user_id,))
            conn.execute("DELETE FROM generated_items WHERE owner_user_id=?", (user_id,))
            conn.execute("DELETE FROM mcp_context_relay WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM mcp_connections WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM external_learning_inbox WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM handoff_tokens WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM audit_log WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM auth_rate_limits WHERE key_hash=?", (username_hash,))
            conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            conn.execute(
                "INSERT INTO audit_log(audit_id,user_id,action,resource_type,resource_id,client_type,metadata_json,created_at) VALUES(?,NULL,'auth.account_deleted','account',NULL,NULL,'{}',?)",
                (str(uuid4()), iso(utc_now())),
            )
        return storage_keys

    def rotate_recovery_key(self, principal: AuthPrincipal, password: str) -> str:
        row = self.db.fetchone("SELECT password_hash FROM users WHERE user_id=?", (principal.user_id,))
        if not row:
            raise AuthError("Unknown account")
        try:
            valid = self._ph.verify(row["password_hash"], password)
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not valid:
            raise AuthError("Password is incorrect")
        key = self._new_recovery_key()
        self.db.execute(
            "UPDATE users SET recovery_hash=?,updated_at=? WHERE user_id=?",
            (self._ph.hash(key), iso(utc_now()), principal.user_id),
        )
        self.audit(principal.user_id, "auth.rotate_recovery")
        return key

    def list_sessions(self, principal: AuthPrincipal) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT session_token_id,created_at,expires_at,revoked_at,user_agent FROM auth_sessions WHERE user_id=? ORDER BY created_at DESC",
            (principal.user_id,),
        )
        return [
            {
                "session_id": r["session_token_id"],
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
                "revoked": bool(r["revoked_at"]),
                "current": r["session_token_id"] == principal.session_token_id,
                "user_agent": r["user_agent"],
            }
            for r in rows
        ]

    def revoke_session(self, principal: AuthPrincipal, session_id: str) -> bool:
        count = self.db.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND session_token_id=? AND revoked_at IS NULL",
            (iso(utc_now()), principal.user_id, session_id),
        )
        if count:
            self.audit(principal.user_id, "auth.revoke_session", resource_id=session_id)
        return bool(count)

    def audit(
        self,
        user_id: str | None,
        action: str,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        client_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO audit_log(audit_id,user_id,action,resource_type,resource_id,client_type,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                user_id,
                action,
                resource_type,
                resource_id,
                client_type,
                self.db.dumps(metadata or {}),
                iso(utc_now()),
            ),
        )

    def _issue_session(self, user_id: str, username: str, *, user_agent: str | None) -> dict[str, str]:
        now = utc_now()
        access_exp = now + timedelta(minutes=self.settings.access_token_minutes)
        refresh_exp = now + timedelta(days=self.settings.session_days)
        jti = str(uuid4())
        payload = {
            "sub": user_id,
            "username": username,
            "jti": jti,
            "iat": int(now.timestamp()),
            "exp": int(access_exp.timestamp()),
            "aud": "ovael-web",
            "iss": "ovael",
        }
        token = jwt.encode(payload, self._secret, algorithm="HS256")
        refresh_token = "ovrefresh_" + secrets.token_urlsafe(48)
        self.db.execute(
            """INSERT INTO auth_sessions(
            session_token_id,user_id,created_at,expires_at,refresh_token_hash,refresh_expires_at,user_agent
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                jti, user_id, iso(now), iso(refresh_exp), self.hash_token(refresh_token),
                iso(refresh_exp), (user_agent or "")[:500] or None,
            ),
        )
        return {
            "access_token": token,
            "expires_at": iso(access_exp),
            "refresh_token": refresh_token,
            "refresh_expires_at": iso(refresh_exp),
            "token_type": "bearer",
        }

    @staticmethod
    def _new_recovery_key() -> str:
        # 192 bits of entropy, hexadecimal to avoid ambiguous URL-safe character
        # substitutions reducing the effective alphabet.
        raw = secrets.token_hex(24).upper()
        return "OVAEL-" + "-".join(raw[i : i + 6] for i in range(0, len(raw), 6))

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
