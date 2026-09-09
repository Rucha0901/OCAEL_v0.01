"use client";

import { useMemo, useState } from "react";
import { Check, ChevronDown, Copy, KeyRound, LockKeyhole, PlugZap, ServerCog, ShieldCheck, Trash2 } from "lucide-react";
import { ovaelApi } from "../../../api/client";
import { useOvael } from "../../../context/OvaelContext";
import type { MCPConnection } from "../../../types";

const scopeLabels: Record<string, string> = {
  "learning.context.read": "Read active learning context",
  "learning.external_events.submit": "Submit external learning evidence",
  "learning.map.read": "Read the learning-map projection",
  "learning.materials.search": "Search authorized materials",
  "learning.session.create": "Start a teaching session",
  "learning.session.interact": "Continue a teaching session",
  "learning.suggestions.submit": "Suggest a teaching strategy",
};

function publicMcpUrl(serverUrl?: string) {
  if (typeof window !== "undefined" && ovaelApi.baseUrl.startsWith("/")) return `${window.location.origin}/mcp`;
  return serverUrl || "http://127.0.0.1:8000/mcp";
}

export function ConnectionsScreen() {
  const { connections, refresh, setError } = useOvael();
  const [clientName, setClientName] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [issued, setIssued] = useState<MCPConnection | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState("");
  const active = useMemo(() => connections.filter((connection) => !connection.revoked), [connections]);
  const revoked = useMemo(() => connections.filter((connection) => connection.revoked), [connections]);
  const selected = active.find((connection) => connection.connection_id === selectedId) || active[0];
  const endpoint = publicMcpUrl(issued?.mcp_url);
  const clientConfig = issued?.access_token ? JSON.stringify({
    mcpServers: {
      ovael: {
        type: "http",
        url: endpoint,
        headers: { Authorization: `Bearer ${issued.access_token}` },
      },
    },
  }, null, 2) : "";

  const copy = async (value: string, label: string) => {
    await navigator.clipboard.writeText(value);
    setCopied(label);
    window.setTimeout(() => setCopied(""), 1800);
  };

  const create = async () => {
    if (!clientName.trim()) return;
    setBusy(true); setError("");
    try {
      const result = await ovaelApi.createMcpConnection(clientName.trim());
      setIssued(result); setSelectedId(result.connection_id); setClientName("");
      await refresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : "Connection could not be created");
    } finally { setBusy(false); }
  };

  const revoke = async (connection: MCPConnection) => {
    if (!window.confirm(`Revoke ${connection.client_name}? That client will immediately lose access.`)) return;
    setBusy(true);
    try {
      await ovaelApi.revokeMcpConnection(connection.connection_id);
      if (issued?.connection_id === connection.connection_id) setIssued(null);
      setSelectedId("");
      await refresh();
    } finally { setBusy(false); }
  };

  return <div className="page connections-page">
    <header className="page-heading connections-heading"><div><p className="eyebrow blue">MCP connections</p><h1>Connect another learning tool.</h1><p>Issue one revocable, learner-scoped credential. OVAEL keeps mastery decisions and private conversations outside the connection boundary.</p></div><div className="connection-count"><strong>{active.length}</strong><span>active connection{active.length === 1 ? "" : "s"}</span></div></header>

    <section className="connection-create" aria-labelledby="new-connection-title"><label><span id="new-connection-title">Client name</span><input value={clientName} onChange={(event) => setClientName(event.target.value)} placeholder="For example: Claude Desktop" onKeyDown={(event) => { if (event.key === "Enter") void create(); }} /></label><button className="primary-button" disabled={busy || !clientName.trim()} onClick={() => void create()}><PlugZap size={16} /> {busy ? "Creating…" : "Create connection"}</button></section>

    {issued?.access_token && <section className="mcp-setup" role="status">
      <header><div className="setup-icon"><KeyRound size={19} /></div><div><p className="eyebrow">Connection ready</p><h2>Finish setup in {issued.client_name}</h2><p>This credential is shown once. Copy the complete configuration or save the token before leaving this page.</p></div></header>
      <div className="mcp-setup-grid"><div><span>1 · Streamable HTTP endpoint</span><code>{endpoint}</code><button onClick={() => void copy(endpoint, "endpoint")}><Copy size={14} /> {copied === "endpoint" ? "Copied" : "Copy endpoint"}</button></div><div><span>2 · Bearer token</span><code className="secret-value">{issued.access_token}</code><button onClick={() => void copy(issued.access_token || "", "token")}><Copy size={14} /> {copied === "token" ? "Copied" : "Copy token"}</button></div></div>
      <div className="config-copy"><div><strong>Example client configuration</strong><small>Use this for clients that accept an HTTP MCP server with request headers.</small></div><button className="secondary-button compact" onClick={() => void copy(clientConfig, "config")}><Copy size={14} /> {copied === "config" ? "Configuration copied" : "Copy full configuration"}</button></div>
      <pre><code>{clientConfig}</code></pre>
    </section>}

    <div className="connections-layout">
      <section className="connection-list" aria-label="Active MCP connections">
        <div className="connection-list-title"><div><p className="eyebrow">Active clients</p><h2>{active.length ? "Connected tools" : "No tool currently has access"}</h2></div><span>{active.length}</span></div>
        {active.map((connection) => <button key={connection.connection_id} onClick={() => setSelectedId(connection.connection_id)} className={selected?.connection_id === connection.connection_id ? "selected" : ""}><span className="connection-glyph">{connection.client_name.slice(0, 1).toUpperCase()}</span><div><strong>{connection.client_name}</strong><small>Expires {new Date(connection.expires_at).toLocaleDateString()}</small></div><span className="connection-state connected">active</span></button>)}
        {!active.length && <div className="connection-empty"><ServerCog size={25} /><p>Create a connection above. The token and ready-to-copy client configuration will appear here once.</p></div>}
        {revoked.length > 0 && <details className="connection-history"><summary><span>Revoked history</span><em>{revoked.length}</em><ChevronDown size={15} /></summary><div>{revoked.map((connection) => <article key={connection.connection_id}><span className="connection-glyph">{connection.client_name.slice(0, 1).toUpperCase()}</span><div><strong>{connection.client_name}</strong><small>Access revoked · created {new Date(connection.created_at).toLocaleDateString()}</small></div></article>)}</div></details>}
      </section>

      <aside className="scope-panel">
        {selected ? <><div className="scope-title"><span className="connection-glyph large">{selected.client_name.slice(0, 1).toUpperCase()}</span><div><p className="eyebrow">Active connection</p><h2>{selected.client_name}</h2></div></div><div className="endpoint-line"><span>MCP endpoint</span><code>{publicMcpUrl()}</code><button aria-label="Copy MCP endpoint" onClick={() => void copy(publicMcpUrl(), "detail-endpoint")}><Copy size={14} /></button></div><h3>Allowed capabilities</h3><ul>{selected.scopes.map((scope) => <li key={scope}><Check size={15} /> {scopeLabels[scope] || scope.replaceAll(/[._]/g, " ")}</li>)}</ul><h3>Never allowed</h3><ul className="denied"><li><LockKeyhole size={15} /> Raw learner conversations</li><li><LockKeyhole size={15} /> Direct learner-state mutation</li><li><LockKeyhole size={15} /> Password or recovery settings</li></ul><button className="secondary-button full" disabled={busy} onClick={() => void revoke(selected)}><Trash2 size={16} /> Revoke connection</button></> : <><div className="scope-title"><span className="connection-glyph large"><ShieldCheck size={19} /></span><div><p className="eyebrow">Permission boundary</p><h2>Private by default</h2></div></div><div className="mcp-how"><div><span>1</span><p><strong>Name the client</strong>Use a label you will recognize later.</p></div><div><span>2</span><p><strong>Copy the credential once</strong>OVAEL stores only its secure hash.</p></div><div><span>3</span><p><strong>Revoke at any time</strong>The client loses access immediately.</p></div></div></>}
      </aside>
    </div>

    <div className="capability-note compact-note"><ShieldCheck size={18} /><div><strong>Backend-enforced scope boundary</strong><p>The MCP server exposes learning context, authorized material search, teaching sessions, and learner-safe suggestions. It cannot read passwords or directly set mastery.</p></div></div>
  </div>;
}
