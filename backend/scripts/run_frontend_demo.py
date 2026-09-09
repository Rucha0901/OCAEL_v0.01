"""Run the packaged Bharat demo safely for the OVAEL web frontend."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import uvicorn


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
RUNTIME.mkdir(exist_ok=True)
DB_PATH = RUNTIME / "ovael-frontend-demo.db"
if not DB_PATH.exists():
    shutil.copy2(ROOT / "demo" / "bharat_demo.db", DB_PATH)

os.environ.setdefault("ADAPTIVE_DB_PATH", str(DB_PATH))
os.environ.setdefault("OVAEL_STORAGE_DIR", str(RUNTIME / "uploads"))
os.environ.setdefault("OVAEL_AUTH_REQUIRED", "true")
os.environ.setdefault("OVAEL_AUTH_SECRET", "ovael-local-demo-secret-change-before-production-2026")
os.environ.setdefault("OVAEL_MEMORY_MODE", "local_first")
os.environ.setdefault("OVAEL_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001")
os.environ.setdefault("OVAEL_TRUSTED_HOSTS", "localhost,127.0.0.1,::1,testserver")
os.environ.setdefault("OVAEL_PUBLIC_BASE_URL", "http://127.0.0.1:8000")

sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    uvicorn.run("adaptive_backend.app:create_app", factory=True, host="127.0.0.1", port=8000, reload=False)
