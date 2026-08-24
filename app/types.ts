export type Role = "learner" | "teacher";

export type LearnerScreen =
  | "learn"
  | "call"
  | "chat"
  | "graph"
  | "gaps"
  | "materials"
  | "history"
  | "connections"
  | "settings";

export interface Health {
  status: string;
  product: string;
  version: string;
  database: string;
  mimo_configured: boolean;
  ocr_configured?: boolean;
  stt_configured?: boolean;
  tts_configured?: boolean;
  voice_transport?: "turn_based";
  live_call_supported?: boolean;
  mcp: string;
  dynamic_teaching: string;
  agents: string[];
}

export interface LearningLaunch {
  subjectId?: string;
  conceptId?: string;
  conceptName?: string;
  courseId?: string;
  documentId?: string;
  documentName?: string;
}

export interface AuthUser {
  user_id: string;
  username: string;
  role: Role;
  created_at?: string;
}

export interface AuthResult {
  access_token: string;
  token_type?: string;
  expires_in?: number;
  user_id: string;
  username: string;
  role: Role;
  recovery_key?: string;
}

export interface UserProfile {
  user_id?: string;
  username?: string;
  role?: Role;
  display_name?: string;
  institution?: string;
  bio?: string;
  preferences?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface Subject {
  subject_id: string;
  name: string;
  description?: string | null;
}

export interface ConceptNode {
  id: string;
  label: string;
  kind: string;
  status: string;
  subject_id?: string | null;
}

export interface ConceptEdge {
  source: string;
  target: string;
  relation: string;
}

export interface LearningMap {
  nodes: ConceptNode[];
  edges: ConceptEdge[];
}

export interface TeachingMove {
  move_id: string;
  action: "explain" | "ask" | "hint" | "diagnose" | "contrast" | "worked_example" | "practice" | "prerequisite_rewind" | "reassess" | "summarize";
  target_concept_id: string;
  target_difficulty: number;
  pedagogical_goal: string;
  content: string;
  answer_type?: "free_text" | "mcq" | "boolean" | "numeric" | "code" | null;
  options: string[];
  expected_evidence: string;
  learner_reason: string;
  citations: string[];
  validation_state: string;
}

export interface TeachingSession {
  teaching_session_id: string;
  subject_id: string;
  course_id?: string | null;
  concept_id: string;
  goal?: string | null;
  mode: "study" | "practice" | "review" | "calibration";
  source_client: string;
  status: string;
  turn_count?: number;
  created_at?: string;
  updated_at?: string;
  ended_at?: string | null;
  move?: TeachingMove;
  state_delta?: Record<string, unknown>;
  evidence?: Record<string, unknown>;
  agent_summary?: Record<string, unknown>;
  gap_update?: CausalGapReport;
}

export interface DocumentRecord {
  document_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  status: string;
  page_count?: number | null;
  course_id?: string | null;
  course_name?: string | null;
  subject_id?: string | null;
  subject_name?: string | null;
  focus_concept_id?: string | null;
  focus_concept_name?: string | null;
  version?: number;
  created_at: string;
  updated_at: string;
  error_message?: string | null;
  duplicate?: boolean;
}

export interface Course {
  course_id: string;
  name: string;
  subject_id: string;
  subject_name?: string;
  description?: string | null;
  course_kind: "personal" | "class";
  role?: string;
  created_at?: string;
}

export interface MCPConnection {
  connection_id: string;
  client_name: string;
  scopes: string[];
  created_at: string;
  expires_at: string;
  revoked: boolean;
  access_token?: string;
  mcp_url?: string;
}

export interface MemorySettings {
  personalization_enabled: boolean;
  memory_mode: "local_first" | "server";
  [key: string]: unknown;
}

export interface TeacherGap {
  concept_id?: string;
  concept_name?: string;
  learner_count?: number;
  status?: string;
  suggested_action?: string;
  [key: string]: unknown;
}

export interface CausalEvidenceSignal {
  signal: string;
  detail: string;
  strength: number;
}

export interface CausalGraphNode {
  id: string;
  label: string;
  role: "focus" | "cause" | "upstream" | "downstream";
  state: string;
  value: number;
}

export interface CausalGraphEdge {
  source: string;
  target: string;
  relation: string;
  support: number;
}

export interface CausalGapCase {
  case_id: string;
  status: string;
  subject_id: string;
  target_concept_id: string;
  concept_name: string;
  concept_state: string;
  mastery_belief: number;
  state_confidence: number;
  evidence_count: number;
  recurrence_count: number;
  cause_type: string;
  cause_label: string;
  normalized_support: number;
  confidence: number;
  calibration_state: string;
  abstained: boolean;
  severity: "high" | "medium" | "watch";
  focus_priority: number;
  focus_allocation_pct: number;
  recommended_minutes: number;
  learner_message: string;
  evidence_quality: { score: number; independent_attempts: number; raw_response_returned: boolean };
  evidence_for: CausalEvidenceSignal[];
  evidence_against: string[];
  falsification_check: string;
  next_step: { action: string; title: string; detail: string };
  micrograph: { nodes: CausalGraphNode[]; edges: CausalGraphEdge[]; caption: string };
  challenge?: { reason: string; recorded_at: string; effect: string };
}

export interface CausalGapReport {
  generated_at: string;
  calibration_state: string;
  subject_id?: string | null;
  summary: {
    case_count: number;
    high_priority_count: number;
    abstained_count: number;
    headline: string;
    privacy: string;
  };
  cases: CausalGapCase[];
  agent_trace?: {
    orchestrator: string;
    lead: string;
    specialists: Array<Record<string, unknown>>;
    specialist_budget: { used: number; max: number };
  };
}
