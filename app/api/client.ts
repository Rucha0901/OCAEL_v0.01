import type {
  AuthResult,
  AuthUser,
  CausalGapCase,
  CausalGapReport,
  Course,
  DocumentRecord,
  Health,
  LearningMap,
  MCPConnection,
  MemorySettings,
  Subject,
  TeachingSession,
  UserProfile,
} from "../types";

const configuredBase = process.env.NEXT_PUBLIC_OVAEL_API_BASE?.replace(/\/$/, "");
const browserHost = typeof window === "undefined" ? "" : window.location.hostname;
const publicGatewayBase = browserHost && !["localhost", "127.0.0.1"].includes(browserHost)
  ? "/api/ovael/v1"
  : "http://127.0.0.1:8000/v1";
const API_BASE = configuredBase || publicGatewayBase;
let accessToken = "";

export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: string) {
    super(message);
    this.name = "ApiError";
  }
}

function errorMessage(body: unknown, status: number) {
  if (typeof body === "object" && body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (typeof detail === "object" && detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === "string") return message;
    }
  }
  return `OVAEL request failed (${status})`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "include" });
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body && typeof body === "object" ? (body as { detail?: { code?: string } }).detail : undefined;
    throw new ApiError(errorMessage(body, response.status), response.status, detail?.code);
  }
  return body as T;
}

async function requestBlob(path: string): Promise<Blob> {
  const headers = new Headers();
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(`${API_BASE}${path}`, { headers, credentials: "include" });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(errorMessage(body, response.status), response.status);
  }
  return response.blob();
}

export const ovaelApi = {
  baseUrl: API_BASE,
  setAccessToken(token: string) { accessToken = token; },
  clearAccessToken() { accessToken = ""; },
  health: () => request<Health>("/health"),
  login: (username: string, password: string) => request<AuthResult>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  register: (username: string, password: string, role: "learner" | "teacher") => request<AuthResult>("/auth/register", { method: "POST", body: JSON.stringify({ username, password, role }) }),
  me: () => request<AuthUser>("/auth/me"),
  profile: () => request<UserProfile>("/profile"),
  logout: () => request<{ logged_out: boolean }>("/auth/logout", { method: "POST" }),
  authSessions: () => request<Array<Record<string, unknown>>>("/auth/sessions"),
  revokeAuthSession: (sessionId: string) => request<{ revoked: boolean }>(`/auth/sessions/${sessionId}`, { method: "DELETE" }),
  changePassword: (currentPassword: string, newPassword: string) => request<{ changed: boolean }>("/auth/change-password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) }),
  rotateRecoveryKey: (password: string) => request<{ recovery_key: string }>("/auth/recovery-key", { method: "POST", body: JSON.stringify({ password }) }),
  subjects: () => request<Subject[]>("/subjects"),
  courses: () => request<Course[]>("/courses"),
  createCourse: (payload: { name: string; subject_id?: string; subject_name?: string; description?: string; course_kind: "personal" | "class" }) => request<Course>("/courses", { method: "POST", body: JSON.stringify(payload) }),
  learningMap: (subjectId?: string) => request<LearningMap>(`/memory/map${subjectId ? `?subject_id=${encodeURIComponent(subjectId)}` : ""}`),
  causalGaps: (subjectId?: string) => request<CausalGapReport>(`/gaps/causal${subjectId ? `?subject_id=${encodeURIComponent(subjectId)}` : ""}`),
  analyzeCausalGaps: (subjectId?: string, maxCases = 6) => request<CausalGapReport>("/gaps/causal/analyze", { method: "POST", body: JSON.stringify({ subject_id: subjectId || null, max_cases: maxCases }) }),
  challengeCausalGap: (caseId: string, reason: string) => request<CausalGapCase>(`/gaps/causal/${caseId}/challenge`, { method: "POST", body: JSON.stringify({ reason }) }),
  causalGapPlot: (caseId: string) => requestBlob(`/gaps/causal/${caseId}/plot.png`),
  learningSessions: (limit = 50) => request<TeachingSession[]>(`/learning/sessions?limit=${limit}`),
  learningSession: (sessionId: string) => request<TeachingSession>(`/learning/sessions/${sessionId}`),
  startLearning: async (payload: { subject_id: string; concept_id: string; course_id?: string; goal?: string; mode: "study" | "practice" | "review" | "calibration"; context_capsule?: Record<string, unknown> }) => {
    const result = await request<TeachingSession>("/learning/sessions", { method: "POST", body: JSON.stringify({ ...payload, source_client: "web" }) });
    return { ...result, subject_id: result.subject_id || payload.subject_id, concept_id: result.concept_id || payload.concept_id, course_id: result.course_id ?? payload.course_id, mode: result.mode || payload.mode, source_client: result.source_client || "web", status: result.status || "active", turn_count: result.turn_count ?? 0 };
  },
  learningTurn: (sessionId: string, payload: Record<string, unknown>, idempotencyKey: string) => request<TeachingSession>(`/learning/sessions/${sessionId}/turns`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(payload) }),
  completeLearning: (sessionId: string) => request<TeachingSession>(`/learning/sessions/${sessionId}/complete`, { method: "POST" }),
  documents: (courseId?: string) => request<DocumentRecord[]>(`/documents${courseId ? `?course_id=${encodeURIComponent(courseId)}` : ""}`),
  documentPage: (documentId: string, page = 1) => request<{ document_id: string; page_no: number; status: string; text: string; provenance: Record<string, unknown> }>(`/documents/${documentId}/pages/${page}`),
  uploadDocument: (file: File, courseId?: string, autoRoute = false) => { const form = new FormData(); form.append("file", file); const params = new URLSearchParams(); if (courseId) params.set("course_id", courseId); if (autoRoute) params.set("auto_route", "true"); return request<DocumentRecord>(`/documents/upload${params.size ? `?${params}` : ""}`, { method: "POST", body: form }); },
  routeDocument: (documentId: string) => request<DocumentRecord>(`/documents/${documentId}/route`, { method: "POST" }),
  deleteDocument: (documentId: string) => request<{ deleted: boolean }>(`/documents/${documentId}`, { method: "DELETE" }),
  retrieval: (payload: { query: string; subject_id: string; concept_ids?: string[]; course_id?: string; limit?: number }) => request<Array<Record<string, unknown>>>("/retrieval/search", { method: "POST", body: JSON.stringify(payload) }),
  memorySettings: () => request<MemorySettings>("/memory/settings"),
  setPersonalization: (enabled: boolean) => request<MemorySettings>("/memory/personalization", { method: "POST", body: JSON.stringify({ enabled }) }),
  deleteMemory: (deleteAll = true) => request<Record<string, unknown>>("/memory", { method: "DELETE", body: JSON.stringify({ delete_all: deleteAll }) }),
  mcpConnections: () => request<MCPConnection[]>("/connections/mcp"),
  createMcpConnection: (clientName: string) => request<MCPConnection>("/connections/mcp", { method: "POST", body: JSON.stringify({ client_name: clientName }) }),
  revokeMcpConnection: (connectionId: string) => request<{ revoked: boolean }>(`/connections/mcp/${connectionId}`, { method: "DELETE" }),
  startVoiceCall: (teachingSessionId: string, playbackRate: number) => request<Record<string, unknown>>("/voice/calls", { method: "POST", body: JSON.stringify({ teaching_session_id: teachingSessionId, playback_rate: playbackRate }) }),
  setVoiceSpeed: (callId: string, playbackRate: number) => request<Record<string, unknown>>(`/voice/calls/${callId}/speed?playback_rate=${playbackRate}`, { method: "PATCH" }),
  voiceTurn: (callId: string, audio: Blob, idempotencyKey: string) => { const form = new FormData(); form.append("audio", audio, "utterance.webm"); return request<Record<string, unknown>>(`/voice/calls/${callId}/turn`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: form }); },
  endVoiceCall: (callId: string) => request<Record<string, unknown>>(`/voice/calls/${callId}/end`, { method: "POST" }),
  teacherLearners: (courseId: string) => request<Array<Record<string, unknown>>>(`/courses/${courseId}/learners`),
  teacherGaps: (courseId: string) => request<Record<string, unknown>>(`/teacher/courses/${courseId}/gaps`),
  enrollLearner: (courseId: string, learnerUsername: string) => request<Record<string, unknown>>(`/courses/${courseId}/enroll`, { method: "POST", body: JSON.stringify({ learner_username: learnerUsername }) }),
  graphProposals: (courseId: string) => request<Array<Record<string, unknown>>>(`/courses/${courseId}/graph/proposals`),
  proposeGraph: (courseId: string, documentIds: string[], objective?: string) => request<Record<string, unknown>>(`/courses/${courseId}/graph/proposals`, { method: "POST", body: JSON.stringify({ document_ids: documentIds, objective }) }),
  approveGraph: (courseId: string, revisionId: string) => request<Record<string, unknown>>(`/courses/${courseId}/graph/proposals/${revisionId}/approve`, { method: "POST" }),
  activeCourseGraph: (courseId: string) => request<Record<string, unknown>>(`/courses/${courseId}/graph`),
  shareProgress: (courseId: string, payload: { graph_version: string; concept_states: Array<Record<string, unknown>>; gap_summary: Array<Record<string, unknown>>; shared_with_teacher: boolean }) => request<Record<string, unknown>>(`/courses/${courseId}/progress-snapshot`, { method: "POST", body: JSON.stringify(payload) }),
  systemAgents: () => request<Record<string, unknown>>("/system/agents"),
};

export const integrationNote = "Every learner-state value shown by this UI is received from OVAEL; the browser only stores and presents state deltas.";
