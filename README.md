# OVAEL

**OVAEL is a teaching companion that remembers how a learner understands, detects what is obstructing progress, and chooses the next useful teaching move.**

It combines conversational teaching, turn-based voice learning, uploaded-source instruction, adaptive practice, a personalized learner graph, causal gap forensics, teacher tools, and privacy-scoped MCP access in one authenticated product.

OVAEL is teaching-first. A learner can ask a question, scan a page, upload a book, select a concept, or continue a lesson. The backend gathers evidence, updates the learner model, investigates possible gaps, retrieves relevant sources, and returns one coherent teaching response. Agent coordination remains behind the teacher experience.

## Product principles

- **Teach before testing.** Assessment is used to obtain useful evidence, not to turn the product into a quiz feed.
- **Find causes, not labels.** A weak answer is not automatically treated as a weak learner or even a weak concept.
- **Keep subjects dynamic.** Subjects, courses, uploaded materials, and subject agents are created at runtime rather than selected from a fixed frontend list.
- **Ground teaching in evidence.** Uploaded sources, OCR text, course scope, learner state, and retrieval citations remain traceable.
- **Separate responsibilities.** Mastery, diagnosis, pedagogy, retrieval, and orchestration have explicit owners.
- **Abstain when evidence is weak.** OVAEL requests a clean diagnostic signal instead of inventing certainty.
- **Protect learner memory.** Raw conversations are not exposed through graph, teacher, MCP, or gap-forensics views.
- **Keep the interface calm.** The product uses a restrained warm-white, ink, cobalt, and jade system with progressive disclosure and accessible controls.

## Learner experience

### Learn

Learn is the primary workspace. It selects the next useful concept from backend-owned evidence or opens an exact concept chosen from My Graph, Gap Forensics, or Materials & OCR.

A teaching session alternates explanation, application, diagnosis, repair, worked examples, and reassessment. The frontend renders the server-selected move and never calculates mastery or teaching difficulty itself.

### Call OVAEL

Call OVAEL provides turn-based voice teaching:

1. capture or upload a learner utterance;
2. transcribe it through the configured STT service;
3. send it through the same teaching engine used by text learning;
4. synthesize the response through TTS;
5. play it at the learner-selected speed.

Browser transcription and read-aloud are available as fallbacks. Continuous interruption-capable live audio is represented separately from the supported turn-based lifecycle.

### Chat with OVAEL

Chat supports open questions, explanations, examples, review, and conversational practice. It uses the selected subject and concept as grounding rather than as a rigid quiz constraint. Responses support Markdown, lists, emphasis, tables, and code without exposing formatting markers as plain text.

### My Graph

My Graph is a server-projected view of concept state and relationships. It displays verified, recovered, developing, review, blocked, stale, and unknown concepts. Selecting **Learn this now** creates an explicit subject-and-concept launch; the Learn screen does not silently return to an unrelated default concept.

### Gap Forensics

Gap Forensics is the learner-facing surface for **IRIS**, OVAEL's causal learning-gap agent.

Each subject is analysed independently. Concepts without learner evidence are not presented as gaps. A fresh subject analysis reconciles previously stored cases so that obsolete diagnoses disappear rather than remaining as static cards.

Each case contains:

- the affected concept and its current learner state;
- a bounded working cause;
- supporting and contradicting evidence;
- evidence quality and independent-attempt count;
- a rival explanation and falsification check;
- recommended time and focus allocation;
- a compact causal micrograph;
- a repair handoff to the existing teaching flow.

The displayed support value is a normalized heuristic, not a psychometric probability. Low evidence quality forces IRIS to abstain and request another observation.

IRIS runs automatically after each learning turn. In local-first mode it reads the temporary session evidence and persists only a derived, privacy-safe case projection to the learner account. Opening Gap Forensics therefore reflects the subject the learner was actually studying.

### Materials & OCR

Materials are teaching sources rather than passive file storage. OVAEL accepts PDF, DOCX, PPTX, text, Markdown, PNG, JPEG, and WebP files.

The default upload mode is **Detect subject from content**:

1. native text extraction or OCR reads the source;
2. the routing layer compares the content with accessible subjects and concepts;
3. a matching personal course is selected, or a new private subject and course are created;
4. source chunks are indexed with the detected subject and focus concept;
5. **Scan and teach** opens Learn with that exact document, course, subject, and concept scope.

For example, a football-training page creates or selects a Sports scope and teaches from the sports source. It cannot fall back to an unrelated Biology concept simply because Biology appeared first in the subject list.

Existing or legacy uploads can use **Detect and teach** to rebuild their subject, concept, course, and retrieval scope from extracted content.

### History & progress

History presents completed learning sessions, source use, concept changes, intervention summaries, and learner-safe progress evidence. It avoids fabricated aggregate metrics.

### Connections

Connections creates scoped tokens for MCP-capable clients. Allowed scopes support learning context, learning-map reads, material search, session interaction, and suggestions. Raw learner conversations, direct mastery mutation, and account-security controls are never exposed.

### Settings & privacy

Learners can control personalization, accessibility preferences, teaching sharing, session security, password recovery, export, and deletion. Memory deletion also removes derived causal-gap cases.

## Teacher experience

Teachers work with courses rather than unrestricted learner accounts. Teacher surfaces provide:

- course creation and enrollment;
- teaching-material readiness;
- reviewable course-graph proposals;
- approval before proposed graph structure becomes authoritative;
- learner lists and privacy-minimized progress;
- aggregated gap insights without raw responses;
- bounded teaching suggestions that remain advisory to Carl.

## Agent architecture

OVAEL has five lead agents coordinated by X.

| Lead | Ownership | Does not own |
| --- | --- | --- |
| **X** | routing, orchestration, cross-lead consistency, specialist budgets | mastery or pedagogy |
| **Sam** | learner state, mastery, evidence interpretation, adaptive state | teaching prose or intervention policy |
| **Carl** | pedagogy, intervention selection, pacing, repair strategy | learner-state mutation or source retrieval |
| **Trav** | OCR evidence, uploaded sources, RAG, RLM and citation scope | mastery or causal diagnosis |
| **IRIS** | causal gap forensics, evidence quality, rival causes, falsification and repair verification | mastery mutation or teaching selection |

Each lead can request bounded specialists when the evidence requires them. Specialist spawning is purpose-limited, non-recursive, and capped globally and per lead. Dynamic subject agents are compiled server-side from the active subject and course, allowing OVAEL to teach an unlimited range of domains without hard-coded frontend branches.

## Learning data flow

```text
Learner input
    │
    ▼
X orchestrator
    ├── Sam: learner state and diagnosis
    ├── Carl: teaching decision
    ├── Trav: OCR / RAG / RLM evidence
    └── IRIS: causal-gap projection
    │
    ▼
Validated teaching move
    │
    ├── learner-safe state delta
    ├── source citations
    └── subject-specific gap update
```

All mastery, BKT, adaptive-difficulty, evidence-quality, causal-priority, and focus-allocation calculations run on the backend.

## Causal gap model

IRIS considers direct foundation weakness, prerequisite dependency, misconception, retention decay, transfer difficulty, fragile mastery, assessment uncertainty, and insufficient evidence.

Its focus priority is an auditable bounded heuristic:

```text
priority = clamp(
  0.43 × mastery_deficit
  + 0.20 × recurrence_signal
  + 0.17 × downstream_impact
  + 0.20 × state_severity
)
```

Focus allocations are normalized only across active evidence-backed cases in the selected subject. They describe attention allocation, not grades.

## Knowledge and material grounding

Trav combines:

- trusted subject knowledge;
- learner-owned document chunks;
- course restrictions;
- concept filters;
- lexical and semantic retrieval;
- bounded recursive language-model research when enabled;
- citation and provenance metadata.

Course-scoped sessions cannot retrieve from an unrelated course. OCR output becomes searchable knowledge only after extraction and subject routing.

## Memory model

OVAEL supports two memory modes:

- **Local-first** — teaching evidence is evaluated in a session-scoped learner engine. The browser retains bounded state deltas, and the backend retains resumable session pointers and privacy-safe derived projections.
- **Server** — learner evidence and state are persisted for explicit development, benchmark, or controlled deployment scenarios.

The frontend never reimplements learner-state mathematics. It returns bounded state deltas as context when a new session begins.

## Backend API

The authenticated application API is served under `/v1`.

Primary groups include:

- `/auth`, `/profile`, `/subjects`, `/courses`
- `/learning/sessions`
- `/gaps/causal`
- `/memory/map`, `/memory/settings`, `/memory`
- `/documents`, `/retrieval/search`
- `/voice/calls`
- `/connections/mcp`
- `/teacher/courses`
- `/system/agents`, `/system/subject-agents`

Important causal-gap routes:

```text
POST /v1/gaps/causal/analyze
GET  /v1/gaps/causal?subject_id=...
GET  /v1/gaps/causal/{case_id}
POST /v1/gaps/causal/{case_id}/challenge
GET  /v1/gaps/causal/{case_id}/plot.png
```

Important material routes:

```text
POST /v1/documents/upload?auto_route=true
POST /v1/documents/{document_id}/route
GET  /v1/documents/{document_id}/pages/{page_no}
POST /v1/retrieval/search
```

## Technology

### Frontend

- React 19
- TypeScript
- Vinext and Vite
- Cloudflare-compatible server rendering
- React Markdown and GitHub-Flavoured Markdown
- Lucide icons
- hand-authored responsive CSS

### Backend

- Python 3.11+
- FastAPI
- SQLite with WAL mode and foreign-key enforcement
- Pydantic
- Argon2 password hashing and signed access sessions
- Matplotlib for causal evidence exports
- Xiaomi MiMo-compatible model gateway
- MCP server and scoped connection service

## Repository structure

```text
app/
  api/                         typed frontend API client
  components/
    content/                   rich teaching-content rendering
    learner/                   learner shell and product screens
    teacher/                   teacher course workspace
  context/                     authenticated product state
  globals.css                  design system and responsive behavior
  types.ts                     shared frontend contracts

backend/
  src/adaptive_backend/
    agents.py                  X, Sam, Carl, Trav, IRIS and specialists
    teaching.py                shared teaching-session lifecycle
    causal_gap.py              IRIS causal-gap engine and ledger
    learning.py                learner-state and diagnosis engine
    adaptive.py                adaptive difficulty and sequencing
    documents.py               extraction, OCR routing and course graphs
    retrieval.py               source-aware retrieval
    rlm.py                     bounded recursive knowledge layer
    memory.py                  state, maps, deletion and local-first support
    mcp_server.py              scoped MCP application service
    api_v1.py                  authenticated product API
  tests/                       backend unit and integration coverage

tests/                         frontend render and public API integration tests
scripts/                       local gateway and operational helpers
```

## Local development

### Requirements

- Node.js 22.13 or newer
- Python 3.11 or newer

### Install

```bash
npm install
npm run backend:install
```

### Start the backend

```bash
npm run backend:dev
```

The local backend listens on `http://127.0.0.1:8000` and uses an isolated runtime copy of the synthetic Bharat demonstration database.

### Start the frontend

In another terminal:

```bash
npm run dev
```

Open `http://localhost:3000`.

The frontend API base is configured in `.env.example`:

```text
NEXT_PUBLIC_OVAEL_API_BASE=http://localhost:8000/v1
```

### Synthetic demonstration account

```text
Username: Bharat
Password: 12345678
```

The Bharat profile, learning history, personality preferences, learner graph, gap cases, and session records are synthetic product-demo data. They are not psychological claims about a real learner.

## Model and multimodal configuration

The backend model gateway supports Xiaomi MiMo through an OpenAI-compatible endpoint. Runtime configuration can provide the model base URL, model identifier, and API credential without exposing credentials to the browser.

OCR, STT, and TTS readiness are reported by `/v1/health`. The interface enables server-dependent controls only when the corresponding capability is configured. Provider credentials belong in backend environment configuration and must never be committed to source control.

## Validation

Frontend validation:

```bash
npm test
```

This runs TypeScript checking, lint, a production build, and rendered accessibility tests.

Backend validation:

```bash
npm run backend:test
```

Authenticated end-to-end API validation:

```bash
npm run test:integration
```

The integration suite covers authentication, learner graph, subject-specific IRIS analysis, teaching turns, idempotency, voice lifecycle, material routing, OCR/RAG scope, MCP, privacy controls, teacher courses, and gap insights.

## Security and privacy boundaries

- passwords are hashed with Argon2;
- access sessions are signed and revocable;
- uploads are size-limited and validated by file signature;
- Office archives are checked for unsafe paths and expansion limits;
- document retrieval is owner- and course-scoped;
- learner responses are redacted from durable local-first teaching records;
- causal cases contain derived evidence summaries, not raw answers;
- MCP tokens are scoped and revocable;
- teacher views exclude raw learner conversations;
- account and memory deletion include derived gap projections;
- model API credentials remain server-side.

## Deployment

The frontend is compatible with the included Cloudflare/Sites configuration. The Python backend requires its own persistent runtime with SQLite storage, protected environment variables, upload storage, and HTTPS proxying. A single public gateway can route the frontend normally and forward `/api/ovael/*` and `/mcp/*` to the backend.

For production use, replace demonstration secrets, configure trusted hosts and allowed origins, provide persistent backup and monitoring, and choose the intended memory mode explicitly.
