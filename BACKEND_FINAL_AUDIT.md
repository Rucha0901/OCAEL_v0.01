# OVAEL v0.01 — Final Hidden-Architecture Audit

## Release result

This is the final backend-only v0.01 hidden-audit build.

```text
pytest:       63 / 63 passed
compileall:  passed
app factory: passed
/v1/health:  200 OK
route count: 71 total FastAPI routes in smoke build
frontend:    absent
.env files:  absent
```

No real/paid MiMo API request was required for the audit.

## Highest-impact hidden failures corrected

### 1. Local-first memory was only architectural language, not runtime behavior

Previous teaching sessions still risked treating the authenticated cloud user as the persistent learner-memory owner.

Corrected:

- default `OVAEL_MEMORY_MODE=local_first`;
- per-session shadow learner ID;
- bounded typed context hydration;
- raw event-ledger import rejection;
- raw response redaction in long-lived teaching-turn records;
- replayable event envelopes in `state_delta`;
- deterministic event-ID merge semantics;
- shadow-state cleanup on completion/account/privacy deletion;
- server-persisted mode retained only as an explicit development/backward-compatible option.

### 2. Personalization pause did not fully define persistence behavior

Corrected:

- paused personalization ignores inbound historical capsule data;
- returned deltas are marked `persist=false`;
- no persistent event/state delta is returned;
- MCP context relay is cleared/refuses new personalized context while paused.

### 3. Browser sessions had no durable refresh lifecycle

Corrected:

- short-lived access JWT;
- opaque rotating refresh credential;
- refresh token stored only as SHA-256 hash;
- refresh lifetime uses `OVAEL_SESSION_DAYS`;
- compare-and-swap refresh rotation prevents replay of the old credential;
- HttpOnly access/refresh cookies;
- double-submit CSRF for unsafe cookie-authenticated operations.

### 4. Default auth signing behavior was unsafe for deployment

Corrected:

- local non-required mode can generate an ephemeral process secret;
- auth-required deployment requires a sufficiently long explicit secret;
- access-token expiry now respects configuration.

### 5. Authentication/recovery abuse boundaries were incomplete

Corrected:

- rate-limit persistence for login/recovery paths;
- high-entropy recovery keys;
- only recovery-key hashes stored;
- recovery-key rotation after recovery/change as appropriate;
- session revocation/logout-all/account deletion coverage.

### 6. Idempotent teaching retries returned an inconsistent response contract

Corrected in the final hidden audit:

- repeated learner-turn idempotency keys return the same turn/evidence/move **and the same current `state_delta`**;
- deterministic evidence event IDs prevent duplicate learner evidence in normal retries;
- same-process turn commit is serialized.

### 7. Uploaded documents leaked/retained derived data incorrectly

Corrected:

- internal storage key removed from public document response;
- private document deletion removes derived retrieval chunks and jobs;
- per-user/course deduplication;
- private retrieval ownership checks.

### 8. Upload/file handling had parser and signature risks

Corrected:

- extension allowlist;
- PDF/Office/image signature checks;
- Office ZIP central-directory and uncompressed-size limits;
- archive path traversal checks;
- PDF/page and extracted-character caps;
- blank/image-only files become OCR-pending rather than false-ready.

### 9. Uploaded private knowledge was accidentally treated like globally verified truth

Corrected:

- uploaded/private chunks retain unverified provenance;
- owner-private retrieval can still use them;
- globally verified seeded knowledge remains distinguishable.

### 10. Course/teaching/retrieval ownership boundaries were incomplete

Corrected:

- teaching session validates course ownership and subject compatibility;
- retrieval receives the real content owner separately from ephemeral local-first learner-engine ID;
- direct-ID retrieval paths are owner-scoped.

### 11. Generated content could be marked validated too early

Corrected:

- first generation is unvalidated;
- a second validation pass is required before `generated_validated`;
- MCQ duplicate/empty options rejected;
- boolean/numeric answer-key type validation;
- free-text generation requires a grading reference to become strongly gradable;
- requested/generated difficulty mismatch is bounded;
- programming-family generated code tasks have structured code-test validation.

### 12. Model outage made adaptive evidence effectively unusable

Corrected:

- normal path remains dynamic generation;
- verified anchor questions are used only as outage/calibration/test fallback;
- legacy anchors are normalized so a fake MCQ is never shown without options;
- deterministic grading is available for supported fallback keys;
- unsupported open-ended fallback remains deliberately weak/ungraded instead of inventing correctness.

### 13. Calibration conflated hinted/retried evidence with clean evidence

Corrected:

- per-item calibration metadata;
- shared calibration buckets;
- clean independent-attempt count separate from total attempts;
- hinted/retried responses do not become clean calibration evidence.

### 14. Agent spawning could become cost/latency explosion

Corrected:

- X/Sam/Carl/Trav are typed lead roles;
- each lead <=3 child specialists;
- no recursive child spawning;
- configurable global specialist cap <=12;
- independent specialists execute in parallel;
- parent merges specialist results before X receives them.

### 15. Model calls were too tightly coupled to business logic

Corrected:

- centralized model gateway/router;
- MiMo credentials and base/model names only from environment;
- retry only retryable failures;
- client errors fail fast;
- timeout and simple circuit-breaker behavior;
- business modules do not own provider credentials.

### 16. MCP was at risk of becoming only a label over ordinary APIs

Corrected:

- protocol-independent MCP business service;
- thin official-SDK adapter declared through `mcp>=2,<3`;
- bounded tools rather than raw SQL/database access;
- scoped hashed connection credentials;
- bounded Context Relay;
- local-first map projection from relay;
- teaching-session tools use the same OVAEL teaching engine;
- advisory suggestions are actually persisted/consumed but cannot set mastery;
- handoff and External Learning Inbox implemented.

### 17. MCP teaching turns could lose important evidence modalities

Corrected:

`next_teaching_turn` accepts:

- text response;
- selected option;
- numeric value;
- code;
- confidence;
- hint count;
- attempt count;
- response time;
- idempotency key.

### 18. Account/privacy deletion could leave OVAEL shadow state or MCP artifacts

Corrected:

Deletion paths cover:

- auth sessions/credentials;
- teaching sessions/turns;
- local-first shadow session/state rows;
- handoffs;
- external inbox;
- MCP context/connections;
- private content metadata/retrieval derivatives;
- learner state selected by privacy deletion.

## Final regression additions

The hidden-audit test file additionally verifies:

- local-first shadow hydration and cleanup;
- raw-response redaction;
- access/refresh lifecycle and refresh-token replay rejection;
- MCP suggestion consumption as advisory context;
- replayable event envelopes in local-first state delta;
- idempotent retry returns the same state delta;
- personalization pause suppresses persistent delta and MCP relay state.

## Explicit boundaries that remain deployment adapters

The repository intentionally does **not** pretend these are implemented:

1. PostgreSQL/pgvector production persistence/search adapter.
2. S3-compatible presigned multipart upload adapter.
3. Celery/RabbitMQ or another distributed job worker.
4. Production external isolated code-sandbox/microVM service.
5. Full MCP OAuth authorization-server/interactive-consent flow.
6. Optional encrypted multi-device learner-vault backup/sync.
7. Frontend.

The local upload implementation streams to disk, but native extraction is still synchronous behind the durable local job ledger. Retrieval is FTS/lexical + graph-guided concept scoping, not falsely described as pgvector semantic search.

## MCP test caveat

`mcp>=2,<3` is declared as a runtime dependency and the official adapter is implemented, but the current audit container does not have the `mcp` package installed. Therefore automated local MCP transport tests exercised the compatibility fallback/business service, not a live official-SDK Streamable HTTP transport instance.

This limitation is documented rather than hidden.

## Packaging checks required for release ZIP

The final packaging step removes:

- `.pytest_cache`;
- `build/`;
- local `data/` generated during tests;
- all `__pycache__` and `.pyc` files;
- any SQLite/database files;
- any `.env` files.

Tests remain included.

## Release verdict

OVAEL v0.01 is internally consistent as a **backend-only hackathon/local-service release** after the hidden-architecture audit. It should not be marketed as a complete distributed-cloud production stack until the explicit deployment adapters above are implemented.
