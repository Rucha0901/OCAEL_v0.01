from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile


class StorageError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class StoredObject:
    storage_key: str
    size_bytes: int
    sha256: str


class LocalStorage:
    """Streaming private local storage adapter used by the hackathon backend.

    The API is intentionally storage-provider-shaped so a future S3 adapter can
    replace it without changing document business logic. Uploads are streamed to
    disk and never joined in FastAPI memory.
    """

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def save_upload(self, upload: UploadFile, *, max_bytes: int) -> StoredObject:
        key = f"{uuid4().hex}.bin"
        final_path = self.root / key
        temp_path = self.root / f".{key}.part"
        digest = hashlib.sha256()
        total = 0
        try:
            with temp_path.open("wb") as out:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise StorageError("Upload exceeds 100 MiB limit")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            temp_path.replace(final_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        return StoredObject(storage_key=key, size_bytes=total, sha256=digest.hexdigest())

    def path_for(self, storage_key: str) -> Path:
        if not storage_key or "/" in storage_key or "\\" in storage_key or storage_key.startswith("."):
            raise StorageError("Invalid storage key")
        path = (self.root / storage_key).resolve()
        if path.parent != self.root:
            raise StorageError("Invalid storage key")
        return path

    def delete(self, storage_key: str | None) -> None:
        if not storage_key:
            return
        self.path_for(storage_key).unlink(missing_ok=True)
