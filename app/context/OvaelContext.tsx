"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { ApiError, ovaelApi } from "../api/client";
import type { AuthUser, Course, DocumentRecord, Health, LearningMap, MCPConnection, MemorySettings, Subject, TeachingSession, UserProfile } from "../types";

type ConnectionState = "checking" | "online" | "offline";

interface OvaelContextValue {
  connection: ConnectionState;
  health: Health | null;
  user: AuthUser | null;
  profile: UserProfile | null;
  subjects: Subject[];
  map: LearningMap;
  sessions: TeachingSession[];
  documents: DocumentRecord[];
  courses: Course[];
  connections: MCPConnection[];
  memory: MemorySettings | null;
  loading: boolean;
  error: string;
  apiBase: string;
  login(username: string, password: string): Promise<void>;
  logout(): Promise<void>;
  refresh(): Promise<void>;
  setError(message: string): void;
  saveStateDelta(delta?: Record<string, unknown>): void;
  localState: Record<string, unknown>[];
}

const OvaelContext = createContext<OvaelContextValue | null>(null);
const TOKEN_KEY = "ovael.access-token";

function messageFrom(error: unknown) {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "Something went wrong while contacting OVAEL.";
}

export function OvaelProvider({ children }: { children: React.ReactNode }) {
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [health, setHealth] = useState<Health | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [subjects, setSubjects] = useState<Subject[]>([]);
  const [map, setMap] = useState<LearningMap>({ nodes: [], edges: [] });
  const [sessions, setSessions] = useState<TeachingSession[]>([]);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [courses, setCourses] = useState<Course[]>([]);
  const [connections, setConnections] = useState<MCPConnection[]>([]);
  const [memory, setMemory] = useState<MemorySettings | null>(null);
  const [localState, setLocalState] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const clearAuthenticatedData = useCallback(() => {
    setUser(null); setProfile(null); setSubjects([]); setMap({ nodes: [], edges: [] });
    setSessions([]); setDocuments([]); setCourses([]); setConnections([]); setMemory(null); setLocalState([]);
  }, []);

  const loadAuthenticated = useCallback(async () => {
    const me = await ovaelApi.me();
    setUser(me);
    const results = await Promise.allSettled([
      ovaelApi.profile(), ovaelApi.subjects(), ovaelApi.learningMap(), ovaelApi.learningSessions(),
      ovaelApi.documents(), ovaelApi.courses(), ovaelApi.mcpConnections(), ovaelApi.memorySettings(),
    ]);
    const [profileResult, subjectsResult, mapResult, sessionsResult, documentsResult, coursesResult, connectionsResult, memoryResult] = results;
    if (profileResult.status === "fulfilled") setProfile(profileResult.value);
    if (subjectsResult.status === "fulfilled") setSubjects(subjectsResult.value);
    if (mapResult.status === "fulfilled") setMap(mapResult.value);
    if (sessionsResult.status === "fulfilled") setSessions(sessionsResult.value);
    if (documentsResult.status === "fulfilled") setDocuments(documentsResult.value);
    if (coursesResult.status === "fulfilled") setCourses(coursesResult.value);
    if (connectionsResult.status === "fulfilled") setConnections(connectionsResult.value);
    if (memoryResult.status === "fulfilled") setMemory(memoryResult.value);
    const local = sessionStorage.getItem(`ovael.local-state.${me.user_id}`);
    setLocalState(local ? JSON.parse(local) as Record<string, unknown>[] : []);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const nextHealth = await ovaelApi.health();
      setHealth(nextHealth); setConnection("online");
      const token = sessionStorage.getItem(TOKEN_KEY);
      if (token) {
        ovaelApi.setAccessToken(token);
        try { await loadAuthenticated(); } catch (authError) {
          if (authError instanceof ApiError && authError.status === 401) {
            sessionStorage.removeItem(TOKEN_KEY); ovaelApi.clearAccessToken(); clearAuthenticatedData();
          } else throw authError;
        }
      }
    } catch (nextError) {
      setConnection("offline"); setError(messageFrom(nextError));
    } finally { setLoading(false); }
  }, [clearAuthenticatedData, loadAuthenticated]);

  useEffect(() => { const timer = window.setTimeout(() => void refresh(), 0); return () => window.clearTimeout(timer); }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    setLoading(true); setError("");
    try {
      const result = await ovaelApi.login(username, password);
      ovaelApi.setAccessToken(result.access_token);
      sessionStorage.setItem(TOKEN_KEY, result.access_token);
      setUser({ user_id: result.user_id, username: result.username, role: result.role });
      setConnection("online");
      await loadAuthenticated();
    } catch (nextError) {
      setError(messageFrom(nextError)); throw nextError;
    } finally { setLoading(false); }
  }, [loadAuthenticated]);

  const logout = useCallback(async () => {
    try { await ovaelApi.logout(); } catch { /* local logout remains available if the session expired */ }
    sessionStorage.removeItem(TOKEN_KEY); ovaelApi.clearAccessToken(); clearAuthenticatedData();
  }, [clearAuthenticatedData]);

  const saveStateDelta = useCallback((delta?: Record<string, unknown>) => {
    if (!delta || !user) return;
    setLocalState((current) => {
      const next = [...current.slice(-99), { ...delta, received_at: new Date().toISOString() }];
      sessionStorage.setItem(`ovael.local-state.${user.user_id}`, JSON.stringify(next));
      return next;
    });
  }, [user]);

  const value = useMemo<OvaelContextValue>(() => ({
    connection, health, user, profile, subjects, map, sessions, documents, courses, connections, memory,
    loading, error, apiBase: ovaelApi.baseUrl, login, logout, refresh, setError, saveStateDelta, localState,
  }), [connection, health, user, profile, subjects, map, sessions, documents, courses, connections, memory, loading, error, login, logout, refresh, saveStateDelta, localState]);

  return <OvaelContext.Provider value={value}>{children}</OvaelContext.Provider>;
}

export function useOvael() {
  const context = useContext(OvaelContext);
  if (!context) throw new Error("useOvael must be used inside OvaelProvider");
  return context;
}
