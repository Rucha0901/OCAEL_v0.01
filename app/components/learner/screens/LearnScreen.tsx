"use client";

import { useMemo, useState } from "react";
import { ArrowRight, BookMarked, Check, ChevronLeft, Lightbulb, LoaderCircle, Send, Volume2 } from "lucide-react";
import { ovaelApi } from "../../../api/client";
import { RichText } from "../../content/RichText";
import { useOvael } from "../../../context/OvaelContext";
import type { LearnerScreen, LearningLaunch, TeachingMove, TeachingSession } from "../../../types";

const priority = ["active_blocker", "needs_review", "developing", "stale", "unknown", "recovered", "verified"];

export function LearnScreen({ onNavigate, launch }: { onNavigate: (screen: LearnerScreen) => void; launch?: LearningLaunch | null }) {
  const { profile, map, sessions, documents, localState, saveStateDelta, refresh, setError } = useOvael();
  const launchSubjectId = launch?.subjectId;
  const launchConceptId = launch?.conceptId;
  const conceptNodes = useMemo(() => map.nodes.filter((node) => node.kind === "concept" || node.subject_id), [map.nodes]);
  const focus = useMemo(() => {
    const explicitlySelected = launchConceptId ? conceptNodes.find((node) => node.id === launchConceptId) : undefined;
    if (explicitlySelected) return explicitlySelected;
    return [...conceptNodes]
      .filter((node) => !launchSubjectId || node.subject_id === launchSubjectId)
      .sort((a, b) => priority.indexOf(a.status) - priority.indexOf(b.status))[0];
  }, [conceptNodes, launchConceptId, launchSubjectId]);
  const [session, setSession] = useState<TeachingSession | null>(null);
  const [move, setMove] = useState<TeachingMove | null>(null);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const activeForFocus = focus ? sessions.find((item) => item.status === "active" && item.concept_id === focus.id && item.subject_id === focus.subject_id) : undefined;

  const resume = async () => {
    if (!activeForFocus) return;
    setBusy(true); setError("");
    try {
      const result = await ovaelApi.learningSession(activeForFocus.teaching_session_id);
      if (!result.move) throw new Error("The saved lesson has no current teaching move.");
      setSession(result); setMove(result.move); saveStateDelta(result.state_delta);
    } catch (error) { setError(error instanceof Error ? error.message : "Could not resume the lesson"); }
    finally { setBusy(false); }
  };

  const start = async (mode: "study" | "review" = "study") => {
    if (!focus?.subject_id) return;
    setBusy(true); setError("");
    try {
      const result = await ovaelApi.startLearning({ subject_id: focus.subject_id, concept_id: focus.id, course_id: launch?.courseId, mode, goal: launch?.documentName ? `Teach ${focus.label} from ${launch.documentName}` : `Understand ${focus.label}`, context_capsule: { local_state_deltas: localState.slice(-10), launch_document_id: launch?.documentId, launch_document_name: launch?.documentName } });
      setSession(result); setMove(result.move || null); saveStateDelta(result.state_delta);
    } catch (error) { setError(error instanceof Error ? error.message : "Could not start the lesson"); }
    finally { setBusy(false); }
  };

  const submit = async (intent: "answer" | "need_explanation" = "answer") => {
    if (!session || (!answer.trim() && intent === "answer")) return;
    setBusy(true); setFeedback("");
    try {
      const result = await ovaelApi.learningTurn(session.teaching_session_id, { response: answer, confidence: 0.7, hint_count: intent === "need_explanation" ? 1 : 0, attempt_count: 1, learner_intent: intent, input_mode: "text" }, crypto.randomUUID());
      saveStateDelta(result.state_delta); setSession({ ...result, subject_id: session.subject_id, concept_id: session.concept_id, mode: session.mode, source_client: session.source_client, status: result.status || session.status, turn_count: (session.turn_count ?? 0) + 1 }); setMove(result.move || null); setAnswer("");
      const detected = result.gap_update?.summary?.case_count || 0;
      setFeedback(detected > 0
        ? `Your response was evaluated. IRIS is tracking ${detected} evidence-backed gap${detected === 1 ? "" : "s"} in ${session.subject_id}.`
        : result.evidence ? "Your response was evaluated and no supported gap was added for this subject." : "OVAEL adapted the next teaching move.");
      await refresh();
    } catch (error) { setError(error instanceof Error ? error.message : "Could not submit this turn"); }
    finally { setBusy(false); }
  };

  const leave = async () => {
    if (session?.teaching_session_id) await ovaelApi.completeLearning(session.teaching_session_id).catch(() => undefined);
    setSession(null); setMove(null); void refresh();
  };

  if (!session || !move) {
    return <div className="page learn-home">
      <section className="welcome-block"><div><p className="eyebrow blue">{launch?.documentName ? "Material-led lesson" : launchConceptId ? "Selected from your graph" : "Your next teaching move"}</p><h1>{launch?.documentName ? `Learn from ${launch.documentName}` : launchConceptId && focus ? `Learn ${focus.label}.` : `Good to see you, ${String(profile?.display_name || profile?.username || "learner")}.`}</h1><p>{focus ? launch?.documentName ? `OVAEL will keep retrieval inside this material’s course scope and begin with ${focus.label}.` : launchConceptId ? `Your selection is locked to ${focus.label}; the backend will plan this lesson for ${focus.subject_id}.` : `OVAEL found the most useful place to continue: ${focus.label}.` : "Your learner graph is ready for a new teaching session."}</p></div><div className="session-pace"><span>Memory</span><strong>{conceptNodes.length}</strong><small>concept states</small></div></section>
      <section className="continue-panel" aria-labelledby="continue-title">
        <div className="continue-meta"><span className="subject-chip">{focus?.subject_id || "Dynamic subject"}</span><span className="subtle">{activeForFocus ? "Saved lesson ready to resume" : launchConceptId ? "Your selected concept" : "Selected from backend evidence"}</span></div>
        <div className="continue-grid"><div><p className="eyebrow">Current focus</p><h2 id="continue-title">{focus?.label || "Start a fresh learning path"}</h2><p className="lead">{focus ? `Current state: ${focus.status.replaceAll("_", " ")}. OVAEL will choose the explanation, check, or prerequisite rewind on the server.` : "Upload a book or choose any available subject to build a personal learning path."}</p><button className="primary-button" disabled={busy || !focus?.subject_id} onClick={() => void (activeForFocus ? resume() : start())}>{busy ? <LoaderCircle className="spin" size={17} /> : null}{activeForFocus ? `Resume ${focus?.label || "lesson"}` : launchConceptId ? `Start ${focus?.label || "lesson"}` : "Start learning"} <ArrowRight size={17} /></button></div><div className="focus-path" aria-label="Backend learning state">{[focus, ...conceptNodes.filter((node) => node.id !== focus?.id)].filter(Boolean).slice(0, 3).map((node, index) => node && <div className={`path-item ${index === 0 ? "current" : node.status === "verified" || node.status === "recovered" ? "done" : ""}`} key={node.id}><span>{node.status === "verified" || node.status === "recovered" ? <Check size={14} /> : index + 1}</span><div><strong>{node.label}</strong><small>{node.status.replaceAll("_", " ")}</small></div></div>)}</div></div>
        <footer><BookMarked size={16} /><span>{documents.length ? `Grounded by ${documents.filter((doc) => doc.status === "ready").length} ready learner material${documents.length === 1 ? "" : "s"}` : "Upload material to ground lessons in your own sources"}</span></footer>
      </section>
      <section className="home-lower-grid"><div className="plain-section"><div className="section-heading"><div><p className="eyebrow">Your learning map</p><h2>Backend-owned state</h2></div><button className="text-button" onClick={() => onNavigate("graph")}>Open full graph <ArrowRight size={15} /></button></div><div className="change-list">{conceptNodes.slice(0, 3).map((node) => <div key={node.id}><span className={`state-mark ${node.status}`} /><div><strong>{node.label}</strong><small>{node.status.replaceAll("_", " ")}</small></div></div>)}</div></div><div className="plain-section next-section"><p className="eyebrow">Local-first handoff</p><h2>{localState.length} state deltas saved</h2><p>The browser retains bounded learner-state deltas and returns them as context when a new session begins.</p><div className="tiny-source"><BookMarked size={15} /> No mastery math runs in this interface</div></div></section>
    </div>;
  }

  return <div className="session-page"><header className="session-header"><button className="back-button" onClick={() => void leave()}><ChevronLeft size={18} /> Finish session</button><div><p className="eyebrow">{session.subject_id}</p><strong>{move.pedagogical_goal}</strong></div><span className="session-time">Turn {session.turn_count ?? 0} · {session.mode}</span></header><div className="session-layout"><article className="teaching-workspace"><div className="lesson-kicker"><span>{move.action.replaceAll("_", " ")}</span><div><i className="filled" /><i className="filled" /><i /><i /></div></div><RichText markdown={move.content} className="lesson-content" /><p className="lesson-lead">{move.learner_reason}</p><div className="listen-row"><button className="secondary-button compact" onClick={() => { if ("speechSynthesis" in window) { window.speechSynthesis.cancel(); window.speechSynthesis.speak(new SpeechSynthesisUtterance(move.content)); } }}><Volume2 size={16} /> Listen</button><span className="subtle">Browser read-aloud · live voice is in Call OVAEL</span></div><hr />{move.options?.length > 0 && <div className="option-list">{move.options.map((option, index) => <button key={option} onClick={() => setAnswer(option)} className={answer === option ? "selected" : ""}><span>{index + 1}</span>{option}</button>)}</div>}<section className="check-section"><p className="eyebrow blue">Your turn</p><h2>{move.expected_evidence}</h2><label className="answer-box"><span className="sr-only">Your answer</span><textarea value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="Explain it in your own words…" /></label>{feedback && <p className="inline-success" role="status"><Check size={16} /> {feedback}</p>}<div className="answer-actions"><button className="secondary-button" disabled={busy} onClick={() => void submit("need_explanation")}><Lightbulb size={16} /> Explain another way</button><button className="primary-button" disabled={busy || !answer.trim()} onClick={() => void submit()}>{busy ? <LoaderCircle className="spin" size={16} /> : <Send size={16} />} Send response</button></div></section><footer className="source-footer"><BookMarked size={16} /><span><strong>Sources</strong> {move.citations?.length ? move.citations.join(" · ") : move.validation_state.replaceAll("_", " ")}</span></footer></article><aside className="focus-sidebar"><p className="eyebrow">Current focus</p><h2>{focus?.label || move.target_concept_id}</h2><span className={`status-pill ${focus?.status || "developing"}`}>{(focus?.status || "developing").replaceAll("_", " ")}</span><dl><div><dt>OVAEL is checking</dt><dd>{move.expected_evidence}</dd></div><div><dt>Difficulty</dt><dd>{Math.round(move.target_difficulty * 100)}%</dd></div></dl><div className="agent-note"><strong>One coherent teacher</strong><p>The orchestration trace stays behind this move. The UI receives only the validated teaching decision and learner-safe reason.</p></div></aside></div></div>;
}
