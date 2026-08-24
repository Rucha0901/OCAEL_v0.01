"use client";

import { useMemo, useRef, useState } from "react";
import { ArrowUp, Check, ChevronDown, Lightbulb, LoaderCircle, MessageSquareText, RotateCcw, ScanText, Sparkles, X } from "lucide-react";
import { ovaelApi } from "../../../api/client";
import { RichText } from "../../content/RichText";
import { useOvael } from "../../../context/OvaelContext";
import type { TeachingMove, TeachingSession } from "../../../types";

type ChatEntry = { id: string; role: "ovael" | "learner" | "note"; text: string; move?: TeachingMove };
type ChatMode = "study" | "practice" | "review";

const starters = [
  "Explain this from first principles.",
  "What am I most likely misunderstanding here?",
  "Give me an example, then ask me one question.",
];

export function ChatScreen() {
  const { map, localState, courses, subjects, health, saveStateDelta, refresh, setError } = useOvael();
  const concepts = useMemo(() => map.nodes.filter((node) => node.subject_id && (node.kind === "concept" || node.subject_id)), [map.nodes]);
  const [conceptId, setConceptId] = useState(concepts[0]?.id || "");
  const [mode, setMode] = useState<ChatMode>("study");
  const [session, setSession] = useState<TeachingSession | null>(null);
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [ocrBusy, setOcrBusy] = useState(false);
  const [ocrSource, setOcrSource] = useState<{ documentId: string; filename: string; text: string; preview: string; courseId: string } | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const scanRef = useRef<HTMLInputElement>(null);
  const concept = concepts.find((node) => node.id === conceptId) || concepts[0];

  const reset = () => { setSession(null); setEntries([]); setMessage(""); };

  const begin = async (openingMessage?: string) => {
    if (!concept?.subject_id) return;
    setBusy(true); setError("");
    try {
      const started = await ovaelApi.startLearning({
        subject_id: concept.subject_id,
        concept_id: concept.id,
        course_id: ocrSource?.courseId,
        mode,
        goal: openingMessage || `${mode === "practice" ? "Practice" : mode === "review" ? "Review" : "Discuss"} ${concept.label}`,
        context_capsule: {
          local_state_deltas: localState.slice(-10),
          interface: "chat",
          launch_document_id: ocrSource?.documentId,
          launch_document_name: ocrSource?.filename,
        },
      });
      setSession(started);
      saveStateDelta(started.state_delta);
      if (!openingMessage) {
        setEntries(started.move ? [{ id: started.move.move_id, role: "ovael", text: started.move.content, move: started.move }] : []);
        return;
      }
      const learnerEntry: ChatEntry = { id: crypto.randomUUID(), role: "learner", text: openingMessage };
      setEntries([learnerEntry]);
      const result = await ovaelApi.learningTurn(started.teaching_session_id, {
        response: ocrSource ? `${openingMessage}\n\nOCR source text:\n${ocrSource.text.slice(0, 1200)}` : openingMessage,
        learner_intent: mode === "practice" && !openingMessage.trim().endsWith("?") ? "answer" : "ask_question",
        input_mode: "text",
        attempt_count: 1,
        hint_count: 0,
      }, crypto.randomUUID());
      setSession({ ...started, ...result, turn_count: (started.turn_count ?? 0) + 1 });
      if (result.move) setEntries([learnerEntry, { id: result.move.move_id, role: "ovael", text: result.move.content, move: result.move }]);
      saveStateDelta(result.state_delta);
      await refresh();
    } catch (error) {
      setEntries((current) => [...current, { id: crypto.randomUUID(), role: "note", text: "OVAEL could not save that message. Your text is still visible." }]);
      setError(error instanceof Error ? error.message : "Chat could not start");
    } finally {
      setBusy(false);
      window.setTimeout(() => composerRef.current?.focus(), 0);
    }
  };

  const submit = async (intent?: "ask_question" | "need_explanation") => {
    const text = intent === "need_explanation" ? "Please explain this another way before we continue." : message.trim();
    if (!text || busy || !concept) return;
    setMessage("");
    if (!session) { await begin(text); return; }
    setEntries((current) => [...current, { id: crypto.randomUUID(), role: "learner", text }]);
    setBusy(true); setError("");
    try {
      const learnerIntent = intent || (mode === "practice" && !text.endsWith("?") ? "answer" : "ask_question");
      const result = await ovaelApi.learningTurn(session.teaching_session_id, {
        response: ocrSource ? `${text}\n\nOCR source text:\n${ocrSource.text.slice(0, 1200)}` : text,
        learner_intent: learnerIntent,
        input_mode: "text",
        attempt_count: 1,
        hint_count: learnerIntent === "need_explanation" ? 1 : 0,
      }, crypto.randomUUID());
      setSession({ ...session, ...result, subject_id: session.subject_id, concept_id: session.concept_id, mode: session.mode, source_client: session.source_client, turn_count: (session.turn_count ?? 0) + 1 });
      if (result.move) setEntries((current) => [...current, { id: result.move!.move_id, role: "ovael", text: result.move!.content, move: result.move }]);
      saveStateDelta(result.state_delta);
      await refresh();
    } catch (error) {
      setEntries((current) => [...current, { id: crypto.randomUUID(), role: "note", text: "That message was not saved. Try sending it again." }]);
      setError(error instanceof Error ? error.message : "OVAEL could not respond");
    } finally {
      setBusy(false);
      window.setTimeout(() => composerRef.current?.focus(), 0);
    }
  };

  const finish = async () => {
    if (session) await ovaelApi.completeLearning(session.teaching_session_id).catch(() => undefined);
    reset(); void refresh();
  };

  const startFresh = async () => {
    if (session) await ovaelApi.completeLearning(session.teaching_session_id).catch(() => undefined);
    reset();
    await begin();
  };

  const changeContext = (update: () => void) => {
    if (session) void ovaelApi.completeLearning(session.teaching_session_id).catch(() => undefined);
    update();
    setOcrSource(null);
    reset();
  };

  const scanPage = async (file?: File) => {
    if (!file || !concept?.subject_id) return;
    setOcrBusy(true); setError("");
    try {
      const subject = subjects.find((item) => item.subject_id === concept.subject_id);
      const existing = courses.find((course) => course.course_kind === "personal" && course.subject_id === concept.subject_id);
      const courseId = existing?.course_id || (await ovaelApi.createCourse({
        name: `Personal ${subject?.name || concept.subject_id}`,
        subject_id: concept.subject_id,
        description: "Learner-owned OCR and chat material scope",
        course_kind: "personal",
      })).course_id;
      const uploaded = await ovaelApi.uploadDocument(file, courseId);
      if (uploaded.status !== "ready") throw new Error("OVAEL could not finish reading that image.");
      const page = await ovaelApi.documentPage(uploaded.document_id, 1);
      if (!page.text.trim()) throw new Error("No readable text was found in that image.");
      setOcrSource({ documentId: uploaded.document_id, filename: file.name, text: page.text, preview: page.text.slice(0, 220), courseId });
      setMessage("Help me understand this scanned page.");
      await refresh();
      window.setTimeout(() => composerRef.current?.focus(), 0);
    } catch (error) {
      setError(error instanceof Error ? error.message : "OCR could not read that page");
    } finally {
      setOcrBusy(false);
      if (scanRef.current) scanRef.current.value = "";
    }
  };

  return <div className="chat-workbench">
    <aside className="chat-rail" aria-label="Chat setup">
      <div><p className="eyebrow blue">Chat with OVAEL</p><h1>Ask, learn, or work something out.</h1><p>A real teaching conversation grounded in your learner graph and materials.</p></div>
      <label className="chat-control-label">Focus concept<div className="select-wrap"><select value={concept?.id || ""} onChange={(event) => changeContext(() => setConceptId(event.target.value))}><option value="" disabled>Select a concept</option>{concepts.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.status.replaceAll("_", " ")}</option>)}</select><ChevronDown size={15} /></div></label>
      <label className="chat-control-label compact">Conversation style<div className="select-wrap"><select value={mode} onChange={(event) => changeContext(() => setMode(event.target.value as ChatMode))}><option value="study">Ask & learn</option><option value="practice">Practice together</option><option value="review">Review and connect</option></select><ChevronDown size={15} /></div></label>
      {concept && <dl className="chat-context"><div><dt>Current state</dt><dd><span className={`state-mark ${concept.status}`} />{concept.status.replaceAll("_", " ")}</dd></div><div><dt>Subject</dt><dd>{concept.subject_id}</dd></div></dl>}
      <div className="chat-ocr-status"><ScanText size={17} /><div><strong>{health?.ocr_configured ? "OCR ready" : "OCR unavailable"}</strong><span>Photograph a page and teach from it in this chat.</span></div></div>
      <div className="chat-rail-actions">{session ? <><button className="secondary-button full" onClick={() => void startFresh()} disabled={busy}><RotateCcw size={15} /> New chat</button><button className="quiet-button" onClick={() => void finish()} disabled={busy}><X size={15} /> Finish and save</button></> : <button className="primary-button full" onClick={() => void begin()} disabled={!concept || busy}>{busy ? <LoaderCircle className="spin" size={16} /> : <MessageSquareText size={16} />} Start guided chat</button>}</div>
    </aside>
    <section className="chat-conversation" aria-label="Chat with OVAEL">
      <header className="conversation-header"><div><span className={`conversation-presence ${session ? "active" : ""}`} /><div><strong>OVAEL</strong><small>{session ? `${concept?.label} · turn ${(session.turn_count ?? 0) + 1}` : "Grounded teaching conversation"}</small></div></div><span className="conversation-mode">{mode === "study" ? "Ask & learn" : mode}</span></header>
      <div className="conversation-scroll" aria-live="polite">
        {!session && !entries.length && <div className="conversation-empty"><Sparkles size={24} /><h2>What do you want to understand?</h2><p>Type any question below, or begin with one of these prompts. OVAEL uses the selected concept only as grounding—not as a rigid quiz.</p><div className="chat-starters">{starters.map((starter) => <button key={starter} disabled={busy || !concept} onClick={() => void begin(starter)}>{starter}</button>)}</div></div>}
        {entries.map((entry) => <article key={entry.id} className={`conversation-entry ${entry.role}`}><div className="speaker-mark">{entry.role === "learner" ? "You" : entry.role === "note" ? "Note" : "O"}</div><div><div className="entry-label">{entry.role === "learner" ? "You" : entry.role === "note" ? "System note" : `OVAEL · ${entry.move?.action.replaceAll("_", " ") || "teacher"}`}</div>{entry.role === "ovael" ? <RichText markdown={entry.text} /> : <p>{entry.text}</p>}{entry.move?.expected_evidence && <div className="evidence-prompt"><Check size={14} /><span>{entry.move.expected_evidence}</span></div>}{entry.move?.options?.length ? <div className="conversation-options">{entry.move.options.map((option) => <button key={option} onClick={() => { setMessage(option); composerRef.current?.focus(); }} className={message === option ? "selected" : ""}>{option}</button>)}</div> : null}</div></article>)}
        {busy && <div className="thinking-line"><LoaderCircle className="spin" size={15} /> OVAEL is reading the learner state and preparing the next useful reply…</div>}
      </div>
      <footer className="conversation-composer">
        <input ref={scanRef} className="sr-only" type="file" accept="image/*" capture="environment" onChange={(event) => void scanPage(event.target.files?.[0])} />
        {ocrSource && <div className="chat-ocr-source" role="status"><ScanText size={16} /><div><strong>OCR attached · {ocrSource.filename}</strong><span>{ocrSource.preview}</span></div><button aria-label="Remove OCR source from this chat" onClick={() => setOcrSource(null)}>×</button></div>}
        <div className="composer-box"><textarea ref={composerRef} aria-label="Message OVAEL" rows={2} disabled={busy || ocrBusy || !concept} placeholder={concept ? "Ask OVAEL anything about this concept…" : "Choose a concept to begin"} value={message} onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } }} /><button className="composer-send" aria-label="Send message" disabled={busy || ocrBusy || !concept || !message.trim()} onClick={() => void submit()}><ArrowUp size={18} /></button></div>
        <div className="composer-meta"><div><button className="ocr-composer-action" disabled={busy || ocrBusy || !health?.ocr_configured || !concept} onClick={() => scanRef.current?.click()}>{ocrBusy ? <LoaderCircle className="spin" size={14} /> : <ScanText size={14} />} {ocrBusy ? "Reading page…" : "Scan page (OCR)"}</button><button disabled={!session || busy || ocrBusy} onClick={() => void submit("need_explanation")}><Lightbulb size={14} /> Explain differently</button></div><span>Enter to send · Shift+Enter for a new line</span></div>
      </footer>
    </section>
  </div>;
}
