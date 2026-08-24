import assert from "node:assert/strict";

const base = process.env.OVAEL_TEST_API_BASE || "http://127.0.0.1:8000/v1";
const checks = [];
let token = "";

async function request(path, init = {}) {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${base}${path}`, { ...init, headers });
  const body = await response.json().catch(() => null);
  assert.equal(response.ok, true, `${path}: ${response.status} ${JSON.stringify(body)}`);
  return body;
}

async function check(name, action) { await action(); checks.push(name); }

await check("health and voice capability truth", async () => { const health = await request("/health"); assert.equal(health.product, "OVAEL"); assert.equal(health.voice_transport, "turn_based"); assert.equal(health.live_call_supported, false); });
await check("Bharat authentication", async () => { const auth = await request("/auth/login", { method: "POST", body: JSON.stringify({ username: "Bharat", password: "12345678" }) }); token = auth.access_token; assert.equal(auth.role, "learner"); });
await check("profile projection", async () => assert.equal((await request("/profile")).institution, "IIITM Gwalior"));
let map;
await check("personalized graph", async () => { map = await request("/memory/map"); assert.ok(map.nodes.length >= 10); assert.ok(map.edges.length > 0); });
let causalReport;
await check("IRIS causal gap analysis", async () => { causalReport = await request("/gaps/causal/analyze", { method: "POST", body: JSON.stringify({ max_cases: 6 }) }); assert.equal(causalReport.agent_trace.orchestrator, "X"); assert.equal(causalReport.agent_trace.lead, "IRIS"); assert.ok(causalReport.cases.length > 0); assert.ok(causalReport.cases[0].micrograph.nodes.length >= 2); });
await check("IRIS subject-specific gap counts", async () => { const biology = await request("/gaps/causal/analyze", { method: "POST", body: JSON.stringify({ subject_id: "BIO", max_cases: 6 }) }); const dsa = await request("/gaps/causal/analyze", { method: "POST", body: JSON.stringify({ subject_id: "DSA", max_cases: 6 }) }); assert.ok(biology.cases.every((item) => item.subject_id === "BIO")); assert.ok(dsa.cases.every((item) => item.subject_id === "DSA")); assert.notEqual(biology.summary.case_count, dsa.summary.case_count); });
await check("IRIS persisted case ledger", async () => { const stored = await request("/gaps/causal"); assert.ok(stored.cases.some((item) => item.case_id === causalReport.cases[0].case_id)); });
await check("IRIS Matplotlib export", async () => { const headers = new Headers({ Authorization: `Bearer ${token}` }); const response = await fetch(`${base}/gaps/causal/${causalReport.cases[0].case_id}/plot.png`, { headers }); assert.equal(response.ok, true); assert.equal(response.headers.get("content-type"), "image/png"); });
await check("dynamic subjects", async () => assert.ok((await request("/subjects")).length > 0));
let session;
let teachingSubject;
await check("teaching session start", async () => { const concept = map.nodes.find((node) => node.kind === "concept" && node.subject_id) || map.nodes.find((node) => node.subject_id); teachingSubject = concept.subject_id; session = await request("/learning/sessions", { method: "POST", body: JSON.stringify({ subject_id: concept.subject_id, concept_id: concept.id, mode: "study", source_client: "web", context_capsule: {} }) }); assert.ok(session.move.content); });
let turn;
const idempotencyKey = crypto.randomUUID();
await check("teaching turn", async () => { turn = await request(`/learning/sessions/${session.teaching_session_id}/turns`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ response: "The smallest valid input should return directly without another recursive call.", confidence: .7, learner_intent: "answer", input_mode: "text", attempt_count: 1, hint_count: 0 }) }); assert.ok(turn.move.content); assert.equal(turn.gap_update.agent_trace.lead, "IRIS"); assert.equal(turn.gap_update.subject_id, teachingSubject); });
await check("idempotent replay", async () => { const replay = await request(`/learning/sessions/${session.teaching_session_id}/turns`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ response: "The smallest valid input should return directly without another recursive call.", confidence: .7, learner_intent: "answer", input_mode: "text", attempt_count: 1, hint_count: 0 }) }); assert.equal(replay.idempotent_replay, true); });
await check("voice call and browser-transcript fallback", async () => { const call = await request("/voice/calls", { method: "POST", body: JSON.stringify({ teaching_session_id: session.teaching_session_id, playback_rate: 1 }) }); const fallback = await request(`/learning/sessions/${session.teaching_session_id}/turns`, { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ response: "Why does the base case prevent an infinite recursive chain?", learner_intent: "ask_question", input_mode: "voice", attempt_count: 1, hint_count: 0 }) }); assert.ok(fallback.move.content); await request(`/voice/calls/${call.call_id}/speed?playback_rate=1.25`, { method: "PATCH" }); const ended = await request(`/voice/calls/${call.call_id}/end`, { method: "POST" }); assert.equal(ended.status, "ended"); });
await check("learning completion", async () => assert.equal((await request(`/learning/sessions/${session.teaching_session_id}/complete`, { method: "POST" })).status, "completed"));
let document;
let materialCourse;
await check("material teaching scope", async () => { materialCourse = await request("/courses", { method: "POST", body: JSON.stringify({ name: `Personal DSA QA ${Date.now()}`, subject_id: "DSA", course_kind: "personal" }) }); assert.equal(materialCourse.subject_id, "DSA"); });
await check("material upload and indexing", async () => { const form = new FormData(); form.append("file", new Blob(["A recursion base case returns a direct answer for the smallest valid input."], { type: "text/markdown" }), "frontend-integration.md"); document = await request(`/documents/upload?course_id=${materialCourse.course_id}`, { method: "POST", body: form }); assert.equal(document.status, "ready"); assert.equal(document.course_id, materialCourse.course_id); });
await check("material-scoped retrieval", async () => { const hits = await request("/retrieval/search", { method: "POST", body: JSON.stringify({ query: "smallest valid input direct answer", subject_id: "DSA", course_id: materialCourse.course_id, limit: 6 }) }); assert.ok(hits.some((hit) => hit.source_id === document.document_id || hit.document_id === document.document_id)); });
await check("material deletion", async () => assert.equal((await request(`/documents/${document.document_id}`, { method: "DELETE" })).deleted, true));
await check("material teaching scope cleanup", async () => assert.equal((await request(`/courses/${materialCourse.course_id}`, { method: "DELETE" })).deleted, true));
let sportsDocument;
await check("automatic sports material routing", async () => { const form = new FormData(); form.append("file", new Blob(["Football athletes improve match performance through passing drills, team tactics, recovery, fitness, and tournament training."], { type: "text/plain" }), "football-training.txt"); sportsDocument = await request("/documents/upload?auto_route=true", { method: "POST", body: form }); assert.equal(sportsDocument.status, "ready"); assert.equal(sportsDocument.subject_name, "Sports"); assert.ok(sportsDocument.subject_id.startsWith("USR:")); assert.ok(sportsDocument.focus_concept_id); });
await check("sports source-locked retrieval", async () => { const hits = await request("/retrieval/search", { method: "POST", body: JSON.stringify({ query: "football passing drills and match training", subject_id: sportsDocument.subject_id, course_id: sportsDocument.course_id, concept_ids: [sportsDocument.focus_concept_id], limit: 6 }) }); assert.ok(hits.some((hit) => hit.source_id === sportsDocument.document_id)); });
await check("automatic material routing cleanup", async () => { await request(`/documents/${sportsDocument.document_id}`, { method: "DELETE" }); assert.equal((await request(`/courses/${sportsDocument.course_id}`, { method: "DELETE" })).deleted, true); });
let connection;
await check("MCP connection issue", async () => { connection = await request("/connections/mcp", { method: "POST", body: JSON.stringify({ client_name: "Frontend Integration QA" }) }); assert.ok(connection.access_token.startsWith("ovmcp_")); });
await check("MCP connection revoke", async () => assert.equal((await request(`/connections/mcp/${connection.connection_id}`, { method: "DELETE" })).revoked, true));
await check("personalization control", async () => { await request("/memory/personalization", { method: "POST", body: JSON.stringify({ enabled: false }) }); assert.equal((await request("/memory/personalization", { method: "POST", body: JSON.stringify({ enabled: true }) })).personalization_enabled, true); });

const teacherName = `frontendteacher${Date.now()}`;
await check("teacher registration", async () => { const auth = await request("/auth/register", { method: "POST", body: JSON.stringify({ username: teacherName, password: "TeacherPass123!", role: "teacher" }) }); token = auth.access_token; assert.equal(auth.role, "teacher"); });
let course;
await check("dynamic class course", async () => { course = await request("/courses", { method: "POST", body: JSON.stringify({ name: "Frontend QA Course", subject_name: "Systems Thinking", course_kind: "class" }) }); assert.ok(course.subject_id.startsWith("USR:")); });
await check("teacher enrollment", async () => assert.equal((await request(`/courses/${course.course_id}/enroll`, { method: "POST", body: JSON.stringify({ learner_username: "Bharat" }) })).status, "active"));
await check("teacher learner list", async () => assert.ok((await request(`/courses/${course.course_id}/learners`)).some((learner) => learner.username === "Bharat")));
await check("privacy-minimized gap view", async () => assert.match((await request(`/teacher/courses/${course.course_id}/gaps`)).privacy, /no raw/i));

console.log(`OVAEL backend integration: ${checks.length}/${checks.length} checks passed`);
for (const name of checks) console.log(`✓ ${name}`);
