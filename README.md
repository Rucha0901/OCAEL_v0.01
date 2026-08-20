# OVAEL v0.01 — Backend Only

**Hackathon coverage:** PS10 + PS17  
**Release:** `v0.01` / Python package version `0.01.0`  
**Platform:** web-application backend  
**Frontend:** intentionally not included

OVAEL combines learner-gap diagnosis with adaptive personalized teaching. The normal learning path is not a fixed Easy/Medium/Hard bank. It maintains continuous learner/adaptive state, selects a pedagogical action, generates or falls back to validated content, evaluates the learner response, and returns a typed `TeachingMove` plus a replayable learner-state delta.

## Core learning loop

```text
learner response
   ↓
Sam — learner evidence / gaps / prerequisites / uncertainty
   ↓
Carl — next teaching strategy
   ↓
Trav — authorized knowledge / retrieval / tools
   ↓
X — orchestration / conflict check / quality gate
   ↓
TeachingMove
   ↓
learner
   ↺
```

The four lead agents are **X, Sam, Carl, Trav**. A lead may create at most **three temporary specialist children** for a difficult turn. Child specialists cannot recursively spawn children. The total specialist budget per turn is configurable and capped at 12.

## What v0.01 implements

### Learning intelligence

- Existing BKT-style concept-state updates and Bayesian adaptive ability state.
- Continuous target difficulty in `[0,1]` with bounded small-step movement and micro-stage stability.
- Dynamic learning sessions with typed `TeachingMove` output.
- Dynamic assessment generation through the configured model gateway when available.
- A second validation pass before generated assessment content is treated as validated.
- Small verified anchor items used only as model-outage/calibration/test fallback, not as the normal teaching path.
- Deterministic grading for supported fallback free-text, MCQ, boolean, numeric and code-style evidence.
- Generated-item calibration metadata (`provisional → estimated → calibrated`) with hinted/retried attempts excluded from clean independent calibration counts.
- Idempotent learner-turn API; retries return the same turn and state delta instead of double-counting evidence.
- Learner-facing reasons without exposing hidden chain-of-thought.

### Local-first personalization boundary

`OVAEL_MEMORY_MODE=local_first` is the default.

In local-first mode:

- long-lived learner personalization is intended to live in the browser/device vault;
- the server creates a per-teaching-session **ephemeral shadow learner state**;
- only a bounded typed context capsule is hydrated into that shadow state;
- raw event-ledger imports are rejected;
- raw learner response text is not stored long-term in `teaching_turns` (only a hash/presence marker is retained);
- every turn returns a `state_delta` containing concept/adaptive/gap projections and replayable event envelopes;
- event merge semantics are `deduplicate_by_event_id_then_replay_in_timestamp_order`;
- the shadow learner state is scrubbed when the teaching session completes;
- when personalization is paused, history is not hydrated and returned deltas have `persist=false` and `merge_semantics=do_not_persist`.

`OVAEL_MEMORY_MODE=server` remains available for local development/backward-compatible server-persisted flows.

### Authentication

- First-party learner ID / username + password.
- Argon2id password hashes.
- High-entropy recovery keys; only recovery-key hashes are stored.
- Short-lived access JWTs.
- Rotating opaque refresh tokens stored only as hashes.
- Browser mode uses `HttpOnly` access/refresh cookies plus double-submit CSRF protection.
- Refresh-token rotation uses compare-and-swap semantics so an already-rotated token cannot be replayed successfully.
- Login/recovery throttling, session listing/revocation, logout-all, password change, recovery-key rotation and account deletion.
- Deployed `OVAEL_AUTH_REQUIRED=true` mode requires a sufficiently long explicit signing secret.

### Courses, documents and retrieval

- Private user-owned courses.
- 100 MiB upload limit.
- Uploads are streamed to the local storage adapter instead of joining the full body in FastAPI memory.
- Safe generated storage names; uploaded filename never becomes a storage path.
- Per-user/course checksum deduplication.
- File signature checks for PDF, Office ZIP containers, PNG, JPEG and WEBP.
- ZIP-bomb/path-traversal constraints for Office containers.
- Native extraction for PDF, DOCX, PPTX and text/Markdown, with page/character safety caps.
- Blank/image-only material becomes OCR-pending/processing rather than falsely “ready”.
- Document deletion removes derived private retrieval chunks and related jobs before metadata deletion.
- Retrieval is currently **SQLite FTS/lexical + graph-guided concept scoping**, with authorization enforced before private results are returned.
- Uploaded/private material retains unverified provenance; global seeded knowledge may be marked verified.
- Source/document/version/page provenance is carried with retrieval hits when available.

### Jobs and storage

- Durable local job ledger with progress, attempts, cancellation and idempotency keys.
- The local release adapter performs native document extraction synchronously after creating the durable job record.
- This archive does **not** bundle Celery/RabbitMQ, S3 presigned multipart upload or another distributed worker/storage implementation; those are deployment adapters, not falsely claimed features.

### Xiaomi MiMo model gateway

All business modules use one model gateway/router rather than calling a provider directly.

Logical routes include generation, validation, teaching/planning and related model tasks. The current configured provider is OpenAI-compatible Xiaomi MiMo.

No `.env` file is included or required. Use process/deployment environment variables:

```bash
export OVAEL_MIMO_BASE_URL='https://token-plan-sgp.xiaomimimo.com/v1'
export OVAEL_MIMO_MODEL='mimo-v2.5-pro'
export OVAEL_MIMO_API_KEY='YOUR_KEY'
```

If the operator's Xiaomi plan displays a different base URL, configure that value instead. The API key is backend-only and must never be exposed to browser code.

The model gateway includes bounded retries only for retryable failures, timeouts, structured JSON validation and a simple circuit-breaker state. Tests use mocked/no provider calls; no paid model call is required by the suite.

### MCP

MCP is current scope, not a future placeholder.

OVAEL contains a thin official-SDK protocol adapter (`mcp_protocol.py`) and a transport-independent business service (`mcp_server.py`). The package declares `mcp>=2,<3`. If that dependency is unavailable, the local compatibility fallback remains mountable for development/tests.

Core tools:

```text
get_learning_context
search_learning_material
get_learning_map
begin_teaching_session
next_teaching_turn
submit_teaching_suggestion
handoff_to_ovael
complete_external_session
```

MCP properties implemented in this build:

- scoped opaque connection tokens, stored only as hashes;
- per-tool scopes;
- bounded short-lived Context Relay;
- graph projection from the relay in local-first mode;
- advisory teaching suggestions that Carl/X may consume but which cannot directly mutate mastery;
- External Learning Inbox for external-session evidence;
- short-lived owner-bound single-use handoff links;
- external models never receive raw SQL/database credentials or unrestricted raw learner history.

**Important deployment note:** a full browser-based OAuth 2.1 MCP authorization-server/consent flow is **not bundled in v0.01**. The current build uses explicit OVAEL-created scoped personal connection tokens. Do not describe that as OAuth in the frontend or demo.

### Code / OCR / speech

- Existing DSA/static code evidence support is retained.
- Dynamic code tasks can be evaluated through the code-review/sandbox abstraction for supported programming-oriented subjects.
- Untrusted-code execution is disabled by default; production requires an external isolated sandbox adapter.
- OCR and speech adapters remain backend services; OCR is text extraction, not automatic diagram understanding.

## Built-in domains

- DSA & Algorithms
- Programming & Software Engineering
- Game Development
- Biology
- Medical Sciences
- Mathematics
- Physics
- Chemistry
- Computer Systems & Cybersecurity

## Primary API

All new frontend integration should use the versioned `/v1` surface. Important groups:

```text
/v1/auth/*
/v1/subjects
/v1/courses/*
/v1/documents/*
/v1/jobs/*
/v1/retrieval/search
/v1/learning/sessions/*
/v1/memory/*
/v1/connections/mcp/*
/v1/mcp/*
/v1/system/agents
/mcp
```

See [`BACKEND_API_CONTRACT.md`](BACKEND_API_CONTRACT.md) for the exact release routes and behavior.

## Runtime configuration

Typical local run:

```bash
python -m venv .venv
# activate the environment
pip install -e '.[test,ocr]'
uvicorn adaptive_backend.app:create_app --factory --host 127.0.0.1 --port 8000
```

Health:

```text
GET /v1/health
```

Useful OVAEL variables:

```text
OVAEL_AUTH_REQUIRED
OVAEL_AUTH_SECRET
OVAEL_ACCESS_TOKEN_MINUTES
OVAEL_SESSION_DAYS
OVAEL_COOKIE_SECURE
OVAEL_ALLOWED_ORIGINS
OVAEL_TRUSTED_HOSTS
OVAEL_MEMORY_MODE=local_first|server
OVAEL_STORAGE_DIR
OVAEL_MAX_UPLOAD_BYTES
OVAEL_MCP_CONTEXT_TTL_SECONDS
OVAEL_PUBLIC_BASE_URL
OVAEL_AGENT_MAX_TOTAL_SPECIALISTS
OVAEL_MIMO_BASE_URL
OVAEL_MIMO_MODEL
OVAEL_MIMO_API_KEY
```

Legacy `ADAPTIVE_*` variables remain for audited baseline compatibility where relevant.

## Security boundaries

- Identity on protected v1 routes is derived from the authenticated principal, not arbitrary request `user_id` fields.
- Private course/document/retrieval access is owner-scoped.
- Browser unsafe methods require CSRF token validation when cookie authentication is used.
- Passwords, recovery keys, refresh tokens, MCP tokens and handoff tokens are never stored in plaintext.
- Retrieved documents and external teaching suggestions are treated as untrusted data/context, not executable instructions.
- Account deletion cleans auth, learner data, private content metadata, MCP connections/context, teaching sessions and shadow learner state; object-storage cleanup is attempted after the DB deletion transaction.

## Tests

Final v0.01 hidden-audit build:

```text
63 / 63 tests passed
```

Also passed:

- Python `compileall` for `src`;
- FastAPI application factory smoke;
- `GET /v1/health` smoke;
- no frontend files in the release tree;
- no `.env` files in the release tree;
- no packaged SQLite/database files in the final ZIP;
- no literal `sk-...` / `tp-...` provider credentials found by the release scan.

The suite includes the original audited regression tests plus v0.01 tests for authentication/recovery/refresh rotation, private content, streamed documents, dynamic teaching, generated-item validation/calibration, idempotency, local-first shadow memory cleanup, replayable state deltas, personalization pause, agent spawn limits, MiMo routing, MCP scopes/context relay/suggestions/handoff/external inbox and privacy regressions.

## Deployment adapters deliberately not bundled

This is the **final v0.01 hackathon/backend build**, not a claim that every cloud-production adapter exists inside one ZIP. These remain explicit deployment work if the product is taken beyond the hackathon/local service:

- PostgreSQL + pgvector migration/adapter;
- S3-compatible presigned multipart upload adapter;
- distributed background workers/queue such as Celery + RabbitMQ;
- production external code sandbox/microVM service;
- full MCP OAuth authorization server/consent flow;
- optional encrypted multi-device learner-vault sync/backup;
- frontend.

These gaps are kept explicit so the repository does not pretend to implement infrastructure that is not present.

## Audit

Read [`BACKEND_FINAL_AUDIT.md`](BACKEND_FINAL_AUDIT.md) for the hidden architectural failures corrected in this release and the exact remaining boundaries.
