"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  Download,
  Focus,
  RefreshCw,
  Scale,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { ovaelApi } from "../../../api/client";
import { useOvael } from "../../../context/OvaelContext";
import type { CausalGapCase, CausalGapReport, CausalGraphNode, LearningLaunch } from "../../../types";

function percent(value: number) {
  return `${Math.round(value * 100)}%`;
}

function display(value: string) {
  return value.replaceAll("_", " ");
}

function graphPositions(nodes: CausalGraphNode[]) {
  const byRole = new Map<string, CausalGraphNode[]>();
  for (const node of nodes) byRole.set(node.role, [...(byRole.get(node.role) || []), node]);
  const xByRole: Record<string, number> = { upstream: 105, cause: 300, focus: 500, downstream: 705 };
  const positions = new Map<string, { x: number; y: number }>();
  for (const [role, items] of byRole) {
    items.forEach((node, index) => {
      const gap = Math.min(100, 240 / Math.max(items.length, 1));
      positions.set(node.id, { x: xByRole[role] || 300, y: 150 + (index - (items.length - 1) / 2) * gap });
    });
  }
  return positions;
}

function CausalMicrograph({ gap }: { gap: CausalGapCase }) {
  const positions = useMemo(() => graphPositions(gap.micrograph.nodes), [gap]);
  return (
    <figure className="iris-graph">
      <div className="iris-graph-labels" aria-hidden="true"><span>Possible source</span><span>Working cause</span><span>Current focus</span><span>Affected next</span></div>
      <svg viewBox="0 0 810 300" role="img" aria-labelledby={`graph-title-${gap.case_id} graph-desc-${gap.case_id}`}>
        <title id={`graph-title-${gap.case_id}`}>Causal working graph for {gap.concept_name}</title>
        <desc id={`graph-desc-${gap.case_id}`}>{gap.micrograph.caption}</desc>
        <defs><marker id={`arrow-${gap.case_id}`} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" /></marker></defs>
        {gap.micrograph.edges.map((edge, index) => {
          const source = positions.get(edge.source);
          const target = positions.get(edge.target);
          if (!source || !target) return null;
          return <g key={`${edge.source}-${edge.target}-${index}`}><line x1={source.x + 54} y1={source.y} x2={target.x - 54} y2={target.y} markerEnd={`url(#arrow-${gap.case_id})`} /><text x={(source.x + target.x) / 2} y={(source.y + target.y) / 2 - 9}>{display(edge.relation)}</text></g>;
        })}
        {gap.micrograph.nodes.map((node) => {
          const point = positions.get(node.id) || { x: 0, y: 0 };
          const label = node.label.length > 24 ? `${node.label.slice(0, 22)}…` : node.label;
          return <g className={`iris-graph-node ${node.role}`} key={node.id} transform={`translate(${point.x} ${point.y})`}><rect x="-58" y="-29" width="116" height="58" rx="5" /><text textAnchor="middle" y="-3">{label}</text><text className="node-state" textAnchor="middle" y="15">{display(node.state)}</text></g>;
        })}
      </svg>
      <figcaption>{gap.micrograph.caption}</figcaption>
    </figure>
  );
}

function EmptyAnalysis({ onAnalyze, loading }: { onAnalyze: () => void; loading: boolean }) {
  return <section className="iris-empty"><Focus size={28} /><p className="eyebrow blue">No causal case yet</p><h2>IRIS needs evidence before it can explain a gap.</h2><p>Learn or practice first, then run a fresh analysis. IRIS will abstain rather than invent a weakness from missing data.</p><button className="primary-button" onClick={onAnalyze} disabled={loading}>{loading ? "Reviewing evidence…" : "Analyze current evidence"}<ArrowRight size={16} /></button></section>;
}

export function GapForensicsScreen({ onLearn }: { onLearn: (launch: LearningLaunch) => void }) {
  const { subjects, memory } = useOvael();
  const [report, setReport] = useState<CausalGapReport | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [subjectId, setSubjectId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [challengeOpen, setChallengeOpen] = useState(false);
  const [challengeReason, setChallengeReason] = useState("");
  const [actionBusy, setActionBusy] = useState(false);

  const loadStored = useCallback(async (selectedSubject = "") => {
    setLoading(true); setError("");
    try {
      let next = await ovaelApi.causalGaps(selectedSubject || undefined);
      if (next.cases.length === 0 && memory?.personalization_enabled !== false) {
        next = await ovaelApi.analyzeCausalGaps(selectedSubject || undefined);
      }
      setReport(next);
      setSelectedId((current) => next.cases.some((item) => item.case_id === current) ? current : next.cases[0]?.case_id || "");
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "IRIS could not load the gap analysis.");
    } finally { setLoading(false); }
  }, [memory]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadStored(""), 0);
    return () => window.clearTimeout(timer);
  }, [loadStored]);

  const analyze = async (selectedSubject = subjectId) => {
    setLoading(true); setError("");
    try {
      const next = await ovaelApi.analyzeCausalGaps(selectedSubject || undefined);
      setReport(next); setSelectedId(next.cases[0]?.case_id || "");
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "IRIS could not complete the analysis.");
    } finally { setLoading(false); }
  };

  const selected = report?.cases.find((item) => item.case_id === selectedId) || report?.cases[0];

  const challenge = async () => {
    if (!selected || !challengeReason.trim()) return;
    setActionBusy(true); setError("");
    try {
      const updated = await ovaelApi.challengeCausalGap(selected.case_id, challengeReason);
      setReport((current) => current ? { ...current, cases: current.cases.map((item) => item.case_id === updated.case_id ? updated : item) } : current);
      setChallengeOpen(false); setChallengeReason("");
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "The challenge could not be recorded.");
    } finally { setActionBusy(false); }
  };

  const downloadPlot = async () => {
    if (!selected) return;
    setActionBusy(true); setError("");
    try {
      const blob = await ovaelApi.causalGapPlot(selected.case_id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = `iris-${selected.concept_name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}.png`;
      anchor.click(); URL.revokeObjectURL(url);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "The chart could not be exported.");
    } finally { setActionBusy(false); }
  };

  return <div className="iris-page">
    <header className="iris-heading">
      <div><p className="eyebrow blue">IRIS · causal gap forensics</p><h1>Find the obstacle, not a label.</h1><p>OVAEL separates what looks difficult from why it may be difficult—then states what evidence could prove the explanation wrong.</p></div>
      <div className="iris-controls"><label><span>Subject scope</span><select value={subjectId} onChange={(event) => { const value = event.target.value; setSubjectId(value); void analyze(value); }}><option value="">All subjects</option>{subjects.map((subject) => <option key={subject.subject_id} value={subject.subject_id}>{subject.name}</option>)}</select></label><button className="secondary-button" onClick={() => void analyze()} disabled={loading || memory?.personalization_enabled === false}><RefreshCw size={15} className={loading ? "spin" : ""} /> Fresh analysis</button></div>
    </header>

    {memory?.personalization_enabled === false && <div className="iris-notice"><ShieldCheck size={18} /><div><strong>Personalization is paused.</strong><p>Stored cases remain visible, but IRIS will not generate or revise learner evidence until you resume it in Settings & privacy.</p></div></div>}
    {error && <div className="iris-error" role="alert"><AlertCircle size={17} /><span>{error}</span></div>}

    {loading && !report ? <div className="iris-loading" aria-live="polite"><span /><p>IRIS is checking state quality, competing causes, and downstream impact…</p></div> : !report?.cases.length ? <EmptyAnalysis onAnalyze={() => void analyze()} loading={loading} /> : <>
      <section className="iris-brief" aria-label="Gap forensics summary"><div><Sparkles size={17} /><p><span>Current reading</span>{report.summary.headline}</p></div><dl><div><dt>Cases</dt><dd>{report.summary.case_count}</dd></div><div><dt>High focus</dt><dd>{report.summary.high_priority_count}</dd></div><div><dt>IRIS abstained</dt><dd>{report.summary.abstained_count}</dd></div></dl></section>

      <div className="iris-workspace">
        <aside className="iris-case-list" aria-label="Learning gaps to review"><header><div><p className="eyebrow">Focus order</p><h2>Where attention pays off</h2></div><span>{report.cases.length}</span></header>{report.cases.map((item, index) => <button key={item.case_id} className={selected?.case_id === item.case_id ? "active" : ""} onClick={() => { setSelectedId(item.case_id); setChallengeOpen(false); }} aria-pressed={selected?.case_id === item.case_id}><span className="iris-rank">{String(index + 1).padStart(2, "0")}</span><span className="iris-case-copy"><strong>{item.concept_name}</strong><small>{item.abstained ? "Needs a clean check" : item.cause_label}</small><i><b style={{ width: `${item.focus_allocation_pct}%` }} /></i></span><span className="iris-allocation">{item.focus_allocation_pct}%<small>focus</small></span></button>)}<footer><ShieldCheck size={14} />Priority is calculated on the backend from retained evidence. It is not a grade.</footer></aside>

        {selected && <article className="iris-dossier">
          <header className="iris-case-header"><div><div className="iris-status-line"><span className={`iris-severity ${selected.severity}`}>{selected.severity} focus</span><span>{display(selected.concept_state)}</span><span>revision-safe</span></div><h2>{selected.concept_name}</h2><p>{selected.abstained ? "IRIS is holding the causal claim until cleaner evidence arrives." : <>Working cause: <strong>{selected.cause_label}</strong></>}</p></div><div className="iris-time"><strong>{selected.recommended_minutes}</strong><span>minutes<br />recommended</span></div></header>

          <section className="iris-human-message"><Sparkles size={18} /><div><p className="eyebrow">What this means for you</p><p>{selected.learner_message}</p></div></section>

          <section className="iris-metrics" aria-label="Diagnostic confidence"><div><span>Heuristic cause support</span><strong>{percent(selected.normalized_support)}</strong><i><b style={{ width: percent(selected.normalized_support) }} /></i><small>Not a calibrated probability</small></div><div><span>Diagnostic confidence</span><strong>{percent(selected.confidence)}</strong><i><b style={{ width: percent(selected.confidence) }} /></i><small>{selected.evidence_count} retained observations</small></div><div><span>Evidence quality</span><strong>{percent(selected.evidence_quality.score)}</strong><i><b style={{ width: percent(selected.evidence_quality.score) }} /></i><small>{selected.evidence_quality.independent_attempts} independent attempts</small></div></section>

          <section className="iris-section"><div className="iris-section-title"><div><p className="eyebrow">Causal working graph</p><h3>What may be carrying the difficulty</h3></div><button className="quiet-button" onClick={() => void downloadPlot()} disabled={actionBusy}><Download size={15} /> Export chart</button></div><CausalMicrograph gap={selected} /></section>

          <div className="iris-evidence-grid"><section><p className="eyebrow">Evidence supporting this</p>{selected.evidence_for.map((item) => <div className="iris-signal" key={`${item.signal}-${item.detail}`}><CheckCircle2 size={15} /><div><strong>{display(item.signal)}</strong><p>{item.detail}</p></div><span>{percent(item.strength)}</span></div>)}</section><section><p className="eyebrow">Evidence that limits the claim</p>{selected.evidence_against.map((item) => <div className="iris-counter" key={item}><Scale size={15} /><p>{item}</p></div>)}</section></div>

          <section className="iris-next"><div><p className="eyebrow">Smallest useful next move</p><h3>{selected.next_step.title}</h3><p>{selected.next_step.detail}</p></div><button className="primary-button" onClick={() => onLearn({ subjectId: selected.subject_id, conceptId: selected.target_concept_id, conceptName: selected.concept_name })}>Start focused repair <ArrowRight size={16} /></button></section>

          <footer className="iris-case-footer"><div><ShieldCheck size={15} /><span>IRIS explains; Sam still owns mastery, Carl owns teaching, Trav owns sources, and X controls routing.</span></div><button className="text-button" onClick={() => setChallengeOpen((value) => !value)}>This cause does not feel right</button></footer>
          {challengeOpen && <section className="iris-challenge"><label htmlFor="iris-challenge-reason">What should IRIS reconsider?</label><textarea id="iris-challenge-reason" value={challengeReason} onChange={(event) => setChallengeReason(event.target.value)} placeholder="For example: I understood this without help in another course…" /><div><button className="quiet-button" onClick={() => setChallengeOpen(false)}>Cancel</button><button className="secondary-button" disabled={!challengeReason.trim() || actionBusy} onClick={() => void challenge()}>{actionBusy ? "Recording…" : "Record and lower confidence"}</button></div></section>}
        </article>}
      </div>
      <p className="iris-privacy"><ShieldCheck size={14} />{report.summary.privacy} Analysis generated {new Date(report.generated_at).toLocaleString()}.</p>
    </>}
  </div>;
}
