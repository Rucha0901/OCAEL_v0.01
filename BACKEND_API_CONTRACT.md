# OVAEL v0.01 — Backend API Contract

This document describes the frontend-facing v0.01 API. Protected `/v1` identity is derived from the authenticated OVAEL principal; clients do not choose arbitrary user IDs for protected resources.

## Authentication

```text
POST   /v1/auth/register
POST   /v1/auth/login
POST   /v1/auth/refresh
GET    /v1/auth/me
POST   /v1/auth/logout
POST   /v1/auth/logout-all
POST   /v1/auth/change-password
POST   /v1/auth/recover
POST   /v1/auth/recovery-key
GET    /v1/auth/sessions
DELETE /v1/auth/sessions/{session_id}
DELETE /v1/auth/account
```

Browser auth may use:

```text
ovael_session   HttpOnly access cookie
ovael_refresh   HttpOnly rotating refresh cookie
ovael_csrf      readable double-submit CSRF cookie
X-CSRF-Token    required on unsafe cookie-authenticated requests
```

Bearer-token API clients are not subject to browser CSRF checks.

## Subjects

```text
GET /v1/subjects
```

## Courses

```text
POST   /v1/courses
GET    /v1/courses
GET    /v1/courses/{course_id}
PATCH  /v1/courses/{course_id}
DELETE /v1/courses/{course_id}
```

Courses are private and owner-scoped.

## Documents

```text
POST   /v1/documents/upload
GET    /v1/documents
GET    /v1/documents/{document_id}
GET    /v1/documents/{document_id}/pages/{page_no}
DELETE /v1/documents/{document_id}
```

Current v0.01 uses the local streamed-storage adapter. Document responses do not expose internal `storage_key` values.

## Jobs

```text
GET  /v1/jobs
GET  /v1/jobs/{job_id}
POST /v1/jobs/{job_id}/cancel
```

The current build includes the durable local ledger/cancellation contract. A distributed worker backend is a deployment adapter, not bundled.

## Retrieval

```text
POST /v1/retrieval/search
```

Request shape:

```json
{
  "query": "binary search boundary condition",
  "subject_id": "DSA",
  "concept_ids": ["binary_search"],
  "course_id": null,
  "limit": 6
}
```

Retrieval is owner-authorized. Current implementation is SQLite FTS/lexical with graph-guided concept scoping and provenance.

## Learning sessions

```text
GET  /v1/learning/sessions
POST /v1/learning/sessions
GET  /v1/learning/sessions/{session_id}
POST /v1/learning/sessions/{session_id}/turns
POST /v1/learning/sessions/{session_id}/complete
```

Start example:

```json
{
  "subject_id": "BIO",
  "concept_id": "bio_golgi",
  "course_id": null,
  "goal": null,
  "mode": "study",
  "context_capsule": {}
}
```

The central teaching object is `TeachingMove`. A start/turn response also includes `state_delta` in local-first mode.

### Turn inputs

The turn contract can accept:

```text
response
selected_option
numeric_value
code
confidence
hint_count
attempt_count
response_seconds
```

`Idempotency-Key` is required for learner turns. Repeating a completed key returns the same turn ID, evidence, move and state delta rather than creating new evidence.

### Local-first state delta

Typical fields:

```text
memory_mode
subject_id
personalization_enabled
persist
concept_states
adaptive_states
gap_hypotheses
events
merge_semantics
generated_at
```

Normal merge semantics:

```text
deduplicate_by_event_id_then_replay_in_timestamp_order
```

When personalization is paused:

```text
persist=false
merge_semantics=do_not_persist
```

## Memory / privacy

```text
GET    /v1/memory/map
GET    /v1/memory/settings
POST   /v1/memory/personalization
DELETE /v1/memory
```

`DELETE /v1/memory` supports scoped deletion or delete-all according to the request body and authenticated user.

## MCP connections

```text
POST   /v1/connections/mcp
GET    /v1/connections/mcp
DELETE /v1/connections/mcp/{connection_id}
```

The create response returns the opaque scoped MCP connection token once. The stored value is a hash.

The current v0.01 connection mechanism is **not an OAuth authorization-server flow**.

## MCP local-first context / external evidence

```text
POST /v1/mcp/context-relay
GET  /v1/mcp/external-inbox
POST /v1/mcp/external-inbox/{inbox_id}
POST /v1/mcp/handoff/{token}
```

The Context Relay is a short-lived bounded projection, not a raw learner-event database transport.

The External Learning Inbox carries structured evidence/state deltas from external sessions so a local-first client can validate/merge them.

## Remote MCP endpoint

```text
POST /mcp
```

When the official MCP SDK runtime dependency is available, OVAEL mounts the standards-based Streamable HTTP adapter. In the current test container that dependency was not installed, so the legacy compatibility fallback path was what the automated transport tests exercised.

Core OVAEL MCP tools:

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

`next_teaching_turn` accepts the same relevant learner-evidence fields as the native learning-turn API, including text/choice/numeric/code/confidence/hints/retries/response time.

External teaching suggestions are advisory context only and cannot directly mutate mastery.

## Agents

```text
GET /v1/system/agents
```

Leads:

```text
X
Sam
Carl
Trav
```

Per-lead child maximum: 3. Child specialists cannot recursively spawn children.

## Health

```text
GET /v1/health
```

Returns product/version plus local database, model-configuration, MCP-adapter, dynamic-teaching and lead-agent status.
