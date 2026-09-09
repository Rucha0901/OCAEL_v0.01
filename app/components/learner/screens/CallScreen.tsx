"use client";

import { useMemo, useRef, useState } from "react";
import { ArrowUp, Headphones, LoaderCircle, Mic, PhoneOff, ShieldCheck, Volume2 } from "lucide-react";
import { ovaelApi } from "../../../api/client";
import { RichText } from "../../content/RichText";
import { useOvael } from "../../../context/OvaelContext";
import { browserSpeechInputAvailable, startBrowserSpeechInput, type BrowserSpeechHandle } from "../../../lib/browserSpeech";
import { startWavRecording, type WavRecording } from "../../../lib/wavRecorder";

type CallEntry = { id: string; role: "ovael" | "learner" | "note"; text: string; meta?: string };

export function CallScreen() {
  const { sessions, map, health, localState, saveStateDelta, setError } = useOvael();
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [callId, setCallId] = useState("");
  const [teachingSessionId, setTeachingSessionId] = useState("");
  const [liveTranscript, setLiveTranscript] = useState("");
  const [typedTranscript, setTypedTranscript] = useState("");
  const [entries, setEntries] = useState<CallEntry[]>([]);
  const wavRecorder = useRef<WavRecording | null>(null);
  const browserRecorder = useRef<BrowserSpeechHandle | null>(null);
  const browserSpeech = useMemo(() => browserSpeechInputAvailable(), []);
  const serverSpeech = Boolean(health?.stt_configured);
  const speechMode = serverSpeech ? "Server speech" : browserSpeech ? "Browser speech fallback" : "Typed transcript fallback";

  const ensureCall = async () => {
    if (callId && teachingSessionId) return { callId, sessionId: teachingSessionId };
    let sessionId = sessions.find((item) => item.status === "active")?.teaching_session_id || teachingSessionId;
    if (!sessionId) {
      const concept = map.nodes.find((node) => node.subject_id && (node.status === "active_blocker" || node.status === "needs_review")) || map.nodes.find((node) => node.subject_id);
      if (!concept?.subject_id) throw new Error("Begin a lesson or add a subject before starting a voice session.");
      const session = await ovaelApi.startLearning({ subject_id: concept.subject_id, concept_id: concept.id, mode: "study", goal: `Talk through ${concept.label}`, context_capsule: { local_state_deltas: localState.slice(-10) } });
      sessionId = session.teaching_session_id;
      setTeachingSessionId(sessionId);
      saveStateDelta(session.state_delta);
      if (session.move?.content) setEntries([{ id: session.move.move_id, role: "ovael", text: session.move.content, meta: "Opening teaching move" }]);
    } else setTeachingSessionId(sessionId);
    const call = await ovaelApi.startVoiceCall(sessionId, speed);
    const nextCallId = String(call.call_id);
    setCallId(nextCallId);
    return { callId: nextCallId, sessionId };
  };

  const playReply = (text: string, audioBase64?: unknown) => {
    if (typeof audioBase64 === "string" && audioBase64) {
      const audio = new Audio(`data:audio/wav;base64,${audioBase64}`);
      audio.playbackRate = speed;
      void audio.play();
    } else if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = Math.min(2, speed);
      window.speechSynthesis.speak(utterance);
    }
  };

  const sendTranscript = async (transcript: string) => {
    const text = transcript.trim();
    if (!text) return;
    setLiveTranscript(""); setTypedTranscript(""); setBusy(true); setError("");
    try {
      const active = await ensureCall();
      setEntries((current) => [...current, { id: crypto.randomUUID(), role: "learner", text, meta: "Spoken turn" }]);
      const teaching = await ovaelApi.learningTurn(active.sessionId, { response: text, learner_intent: text.endsWith("?") ? "ask_question" : "answer", input_mode: "voice", attempt_count: 1, hint_count: 0 }, crypto.randomUUID());
      const reply = teaching.move?.content || "I recorded that turn. Continue when you are ready.";
      setEntries((current) => [...current, { id: teaching.move?.move_id || crypto.randomUUID(), role: "ovael", text: reply, meta: "Adaptive teaching reply" }]);
      saveStateDelta(teaching.state_delta);
      playReply(reply);
    } catch (error) { setEntries((current) => [...current, { id: crypto.randomUUID(), role: "note", text: "This turn was not saved. Try it again or use the transcript field." }]); setError(error instanceof Error ? error.message : "Voice turn failed"); }
    finally { setBusy(false); }
  };

  const sendServerAudio = async () => {
    setBusy(true); setError("");
    try {
      const audio = await wavRecorder.current?.stop();
      wavRecorder.current = null;
      if (!audio) throw new Error("No audio was recorded.");
      const active = await ensureCall();
      const result = await ovaelApi.voiceTurn(active.callId, audio, crypto.randomUUID());
      const speech = result.transcript as { text?: string; engine?: string } | undefined;
      const teaching = result.teaching as { move?: { move_id?: string; content?: string }; state_delta?: Record<string, unknown> } | undefined;
      const transcript = speech?.text?.trim() || "";
      const reply = teaching?.move?.content || "I recorded that turn. Continue when you are ready.";
      if (transcript) setEntries((current) => [...current, { id: crypto.randomUUID(), role: "learner", text: transcript, meta: speech?.engine || "Server transcript" }, { id: teaching?.move?.move_id || crypto.randomUUID(), role: "ovael", text: reply, meta: "Adaptive teaching reply" }]);
      saveStateDelta(teaching?.state_delta);
      playReply(reply, result.audio_base64);
    } catch (error) { setError(error instanceof Error ? error.message : "Voice turn failed"); }
    finally { setBusy(false); }
  };

  const toggleRecording = async () => {
    setError("");
    if (recording) {
      setRecording(false);
      if (serverSpeech) await sendServerAudio();
      else browserRecorder.current?.stop();
      return;
    }
    try {
      await ensureCall();
      setLiveTranscript("");
      if (serverSpeech) wavRecorder.current = await startWavRecording();
      else if (browserSpeech) browserRecorder.current = startBrowserSpeechInput({
        language: "en-IN",
        onTranscript: (text) => setLiveTranscript(text),
        onEnd: (text) => { browserRecorder.current = null; setRecording(false); if (text) void sendTranscript(text); },
        onError: (message) => { setRecording(false); setError(message); },
      });
      else throw new Error("Microphone transcription is unavailable here. Use the transcript field below—the same teaching engine will respond aloud.");
      setRecording(true);
    } catch (error) { setError(error instanceof Error ? error.message : "Microphone could not start"); }
  };

  const updateSpeed = async (value: number) => { setSpeed(value); if (callId) await ovaelApi.setVoiceSpeed(callId, value).catch(() => undefined); };
  const end = async () => {
    browserRecorder.current?.abort(); browserRecorder.current = null;
    if (recording && wavRecorder.current) await wavRecorder.current.stop().catch(() => undefined);
    if (callId) await ovaelApi.endVoiceCall(callId).catch(() => undefined);
    window.speechSynthesis?.cancel(); setRecording(false); setCallId(""); setTeachingSessionId(""); setLiveTranscript("");
    setEntries((current) => current.length ? [...current, { id: crypto.randomUUID(), role: "note", text: "Voice session ended. The teaching session remains available in History." }] : current);
  };

  return <div className="call-workbench">
    <header className="call-topline"><div><p className="eyebrow blue">Call OVAEL</p><h1>A teaching conversation, spoken one turn at a time.</h1><p>The same learner model handles voice and text. Continuous overlapping WebRTC audio is not presented as available.</p></div><div className="call-capability"><span className={serverSpeech || browserSpeech ? "ready" : "fallback"} /><div><strong>{speechMode}</strong><small>{health?.tts_configured ? "Server voice reply" : "Local browser read-aloud"}</small></div></div></header>
    <section className="call-room" aria-label="Voice teaching room">
      <header><div><span className={`call-presence ${callId ? "active" : ""}`} /><div><strong>{callId ? "Voice session active" : "Ready to call"}</strong><small>Turn-based · authenticated · adaptive</small></div></div>{callId && <button className="end-call" onClick={() => void end()}><PhoneOff size={15} /> End</button>}</header>
      <div className="call-transcript" aria-live="polite">
        {!entries.length && <div className="call-empty"><Headphones size={26} /><h2>Speak naturally. OVAEL keeps the lesson focused.</h2><p>Ask a question, explain an idea, or say that you need another example.</p></div>}
        {entries.map((entry) => <article key={entry.id} className={`call-entry ${entry.role}`}><span>{entry.role === "learner" ? "You" : entry.role === "note" ? "Note" : "O"}</span><div><small>{entry.role === "learner" ? "You" : entry.role === "note" ? "Session" : "OVAEL"}{entry.meta ? ` · ${entry.meta}` : ""}</small>{entry.role === "ovael" ? <RichText markdown={entry.text} /> : <p>{entry.text}</p>}</div></article>)}
        {liveTranscript && <article className="call-entry learner live"><span>You</span><div><small>Listening…</small><p>{liveTranscript}</p></div></article>}
        {busy && <div className="thinking-line"><LoaderCircle className="spin" size={15} /> Preparing the next teaching move…</div>}
      </div>
      <footer className="call-controls"><div className="call-primary-control"><button className={`mic-control ${recording ? "recording" : ""}`} disabled={busy} onClick={() => void toggleRecording()} aria-pressed={recording}>{busy ? <LoaderCircle className="spin" size={22} /> : recording ? <PhoneOff size={22} /> : <Mic size={22} />}</button><div><strong>{busy ? "OVAEL is responding" : recording ? "Listening—tap when finished" : callId ? "Tap to speak" : "Start with your voice"}</strong><small>{serverSpeech ? "Audio is transcribed by the configured backend" : browserSpeech ? "Speech is transcribed by this browser; teaching stays on OVAEL" : "Use the transcript field below"}</small></div></div><label className="speed-control"><Volume2 size={16} /><span>Reply speed</span><select value={speed} onChange={(event) => void updateSpeed(Number(event.target.value))}>{[0.75, 1, 1.25, 1.5, 2].map((value) => <option key={value} value={value}>{value}×</option>)}</select></label></footer>
      <div className="call-text-fallback"><textarea aria-label="Type a transcript instead" rows={2} value={typedTranscript} onChange={(event) => setTypedTranscript(event.target.value)} placeholder="Or type what you want to say…" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendTranscript(typedTranscript); } }} /><button aria-label="Send transcript" disabled={busy || !typedTranscript.trim()} onClick={() => void sendTranscript(typedTranscript)}><ArrowUp size={18} /></button></div>
    </section>
    <div className="call-truth-row"><ShieldCheck size={16} /><span><strong>Available now:</strong> turn-based voice or typed transcript, adaptive teaching, speed control, session history. <strong>Not claimed:</strong> interruptions, overlap, or always-on live audio.</span></div>
  </div>;
}
