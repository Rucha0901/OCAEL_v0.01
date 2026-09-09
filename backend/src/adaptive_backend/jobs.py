from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobService:
    ALLOWED_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}
    TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
    """Durable job ledger with deterministic local execution hooks.

    Real deployments can run workers externally while keeping this database/API
    contract unchanged. The release avoids pretending an in-process thread pool
    is equivalent to a production queue.
    """

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        *,
        user_id: str,
        kind: str,
        resource_id: str | None = None,
        priority: int = 3,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key:
            row = self.db.fetchone(
                "SELECT * FROM jobs WHERE owner_user_id=? AND kind=? AND idempotency_key=?",
                (user_id, kind, idempotency_key),
            )
            if row:
                return self._row(row)
        job_id = str(uuid4())
        now = _now()
        self.db.execute(
            """
            INSERT INTO jobs(job_id,owner_user_id,kind,resource_id,status,priority,progress,idempotency_key,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                user_id,
                kind,
                resource_id,
                "queued",
                max(0, min(4, priority)),
                0.0,
                idempotency_key,
                self.db.dumps(payload or {}),
                now,
                now,
            ),
        )
        return self.get(user_id, job_id)

    def get(self, user_id: str, job_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM jobs WHERE job_id=? AND owner_user_id=?", (job_id, user_id))
        if not row:
            raise ValueError("Unknown job")
        return self._row(row)

    def list(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM jobs WHERE owner_user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(200, limit))),
        )
        return [self._row(r) for r in rows]

    def update(
        self,
        user_id: str,
        job_id: str,
        *,
        status: str | None = None,
        progress: float | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> dict[str, Any]:
        current = self.get(user_id, job_id)
        if current["status"] in self.TERMINAL_STATUSES and status and status != current["status"]:
            raise ValueError("Terminal jobs cannot transition to another status")
        if status is not None and status not in self.ALLOWED_STATUSES:
            raise ValueError("Unknown job status")
        assignments = ["updated_at=?"]
        params: list[Any] = [_now()]
        if status is not None:
            assignments.append("status=?")
            params.append(status)
        if progress is not None:
            assignments.append("progress=?")
            params.append(max(0.0, min(1.0, progress)))
        if result is not None:
            assignments.append("result_json=?")
            params.append(self.db.dumps(result))
        if error is not None:
            assignments.append("error_message=?")
            params.append(error[:4000])
        if increment_attempt:
            assignments.append("attempts=attempts+1")
        params += [job_id, user_id]
        self.db.execute(
            f"UPDATE jobs SET {','.join(assignments)} WHERE job_id=? AND owner_user_id=?",
            tuple(params),
        )
        return self.get(user_id, job_id)

    def cancel(self, user_id: str, job_id: str) -> dict[str, Any]:
        current = self.get(user_id, job_id)
        if current["status"] in self.TERMINAL_STATUSES:
            return current
        self.db.execute(
            "UPDATE jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated_at=? WHERE job_id=? AND owner_user_id=?",
            (_now(), job_id, user_id),
        )
        return self.get(user_id, job_id)

    def _row(self, row: Any) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "resource_id": row["resource_id"],
            "status": row["status"],
            "priority": row["priority"],
            "progress": row["progress"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "cancel_requested": bool(row["cancel_requested"]),
            "payload": self.db.loads(row["payload_json"], {}),
            "result": self.db.loads(row["result_json"], {}),
            "error": row["error_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
