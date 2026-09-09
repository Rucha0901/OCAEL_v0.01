"use client";

import { useMemo, useState } from "react";
import { ArrowRight, Info, Search } from "lucide-react";
import { useOvael } from "../../../context/OvaelContext";
import type { LearningLaunch } from "../../../types";

function uiState(status: string) {
  if (status === "verified") return "stable";
  if (status === "active_blocker" || status === "needs_review" || status === "stale") return "review";
  if (status === "unknown") return "uncertain";
  return status;
}

export function GraphScreen({ onLearn }: { onLearn: (launch: LearningLaunch) => void }) {
  const { map } = useOvael();
  const concepts = useMemo(() => map.nodes.filter((node) => node.kind === "concept" || node.subject_id), [map.nodes]);
  const [selectedId, setSelectedId] = useState("");
  const [query, setQuery] = useState("");
  const visible = concepts.filter((node) => node.label.toLowerCase().includes(query.toLowerCase()));
  const selected = concepts.find((node) => node.id === selectedId) || visible[0] || concepts[0];
  const connected = selected ? map.edges.filter((edge) => edge.source === selected.id || edge.target === selected.id).map((edge) => edge.source === selected.id ? edge.target : edge.source).map((id) => map.nodes.find((node) => node.id === id)?.label).filter(Boolean) : [];

  return <div className="graph-page"><header className="page-heading graph-heading"><div><p className="eyebrow blue">Local learner graph</p><h1>My graph</h1><p>A server-projected map of verified, developing, recovered, and blocked concepts.</p></div><div className="graph-tools"><label><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find a concept" /></label></div></header><div className="graph-layout"><section className="graph-canvas" aria-label="Concept relationship map"><div className="graph-legend"><span><i className="stable" /> Verified</span><span><i className="developing" /> Developing</span><span><i className="review" /> Needs review</span><span><i className="recovered" /> Recovered</span></div><div className="map-area backend-map">{visible.map((node, index) => { const column = index % 4; const row = Math.floor(index / 4); return <button key={node.id} className={`concept-node ${uiState(node.status)} ${selected?.id === node.id ? "selected" : ""}`} style={{ left: `${7 + column * 24}%`, top: `${8 + row * 28}%` }} onClick={() => setSelectedId(node.id)}><span>{node.label}</span><small>{node.status.replaceAll("_", " ")}</small></button>; })}{visible.length === 0 && <div className="empty-state">No concept matches “{query}”.</div>}</div><div className="graph-note"><Info size={15} /> {map.nodes.length} nodes and {map.edges.length} relationships received from the OVAEL memory API. Nothing is scored in the browser.</div></section><aside className="concept-inspector"><p className="eyebrow">Selected concept</p><h2>{selected?.label || "No concept state yet"}</h2>{selected && <span className={`status-pill ${uiState(selected.status)}`}>{selected.status.replaceAll("_", " ")}</span>}<dl><div><dt>Backend state</dt><dd>{selected?.status.replaceAll("_", " ") || "Upload material or begin learning to create evidence."}</dd></div><div><dt>Subject</dt><dd>{selected?.subject_id || "Not assigned"}</dd></div><div><dt>Connected ideas</dt><dd>{connected.length ? connected.join(", ") : "No projected relation yet"}</dd></div></dl><button className="primary-button full" disabled={!selected?.subject_id} onClick={() => selected?.subject_id && onLearn({ conceptId: selected.id, conceptName: selected.label, subjectId: selected.subject_id })}>Learn this now <ArrowRight size={16} /></button></aside></div></div>;
}
