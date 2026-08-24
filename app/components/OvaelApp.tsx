"use client";

import { useEffect, useState } from "react";
import { BookOpen, Cable, ChevronDown, Focus, History, LayoutDashboard, Menu, MessageSquareText, Mic2, Network, RefreshCw, ScanText, Settings, ShieldCheck, Sparkles, X } from "lucide-react";
import type { LearnerScreen, LearningLaunch } from "../types";
import { OvaelProvider, useOvael } from "../context/OvaelContext";
import { LearnerWorkspace } from "./learner/LearnerWorkspace";
import { TeacherWorkspace } from "./teacher/TeacherWorkspace";

const learnerNav: Array<{ id: LearnerScreen; label: string; icon: typeof BookOpen; section?: string }> = [
  { id: "learn", label: "Learn", icon: BookOpen, section: "Learning" },
  { id: "call", label: "Call OVAEL", icon: Mic2 },
  { id: "chat", label: "Chat with OVAEL", icon: MessageSquareText },
  { id: "graph", label: "My graph", icon: Network },
  { id: "gaps", label: "Gap forensics", icon: Focus },
  { id: "materials", label: "Materials & OCR", icon: ScanText, section: "Library" },
  { id: "history", label: "History & progress", icon: History },
  { id: "connections", label: "Connections", icon: Cable, section: "System" },
  { id: "settings", label: "Settings & privacy", icon: Settings },
];

export function OvaelApp() {
  return <OvaelProvider><Product /></OvaelProvider>;
}

function Product() {
  const { user, profile, connection, health, loading, error, apiBase, login, logout, refresh, memory } = useOvael();
  const [screen, setScreen] = useState<LearnerScreen>("learn");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [learningLaunch, setLearningLaunch] = useState<LearningLaunch | null>(null);
  const [username, setUsername] = useState("Bharat");
  const [password, setPassword] = useState("");

  useEffect(() => { window.scrollTo({ top: 0, behavior: "instant" }); }, [user?.role]);

  if (!user) {
    return (
      <main className="auth-page" id="main-content">
        <section className="auth-intro">
          <div className="brand auth-brand"><span className="brand-mark">O</span><span>OVAEL</span></div>
          <p className="eyebrow blue">Teaching companion · learning-gap finder</p>
          <h1>Teaching that remembers what actually changed.</h1>
          <p>Learn from your own books, speak or type, and let OVAEL adapt the next teaching move from evidence—not a generic lesson plan.</p>
          <div className="auth-points"><span>Private learner memory</span><span>Dynamic subjects</span><span>Source-grounded teaching</span></div>
        </section>
        <section className="auth-panel" aria-labelledby="sign-in-title">
          <div className={`backend-state ${connection}`}><i />{connection === "online" ? `Backend connected · v${health?.version ?? "1"}` : connection === "checking" ? "Checking OVAEL backend" : "Backend is not reachable"}</div>
          <p className="eyebrow">Authenticated workspace</p><h2 id="sign-in-title">Sign in to continue</h2>
          <form onSubmit={(event) => { event.preventDefault(); void login(username, password); }}>
            <label>Learner ID<input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} /></label>
            <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
            {error && <p className="form-error" role="alert">{error}</p>}
            <button className="primary-button full" disabled={loading || connection !== "online"}>{loading ? "Connecting…" : "Sign in"}</button>
          </form>
          <button className="demo-login" onClick={() => { setUsername("Bharat"); setPassword("12345678"); }}><Sparkles size={16} /> Fill synthetic Bharat demo</button>
          {connection === "offline" && <div className="offline-help"><p>Start the included Python backend, then retry. The UI is configured for:</p><code>{apiBase}</code><button className="secondary-button compact" onClick={() => void refresh()}><RefreshCw size={15} /> Retry connection</button></div>}
          <p className="auth-privacy"><ShieldCheck size={15} /> Passwords are sent only to the OVAEL API. Access tokens remain in this browser tab.</p>
        </section>
      </main>
    );
  }

  const teacher = user.role === "teacher";
  const navigate = (next: LearnerScreen, launch?: LearningLaunch) => {
    setScreen(next);
    setLearningLaunch(next === "learn" ? launch || null : null);
    setMobileOpen(false);
    window.scrollTo({ top: 0, behavior: "instant" });
  };
  const displayName = String(profile?.display_name || user.username);
  const initials = displayName.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();

  return (
    <div className="app-frame">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <aside className={`side-rail ${mobileOpen ? "is-open" : ""}`} aria-label="Primary navigation">
        <div className="brand-row"><button className="brand" onClick={() => navigate("learn")} aria-label="OVAEL home"><span className="brand-mark">O</span><span>OVAEL</span></button><button className="icon-button close-nav" onClick={() => setMobileOpen(false)} aria-label="Close navigation"><X size={20} /></button></div>
        <nav className="nav-list">
          {!teacher ? learnerNav.map((item) => { const Icon = item.icon; return <div key={item.id}>{item.section && <p className="nav-section">{item.section}</p>}<button className={`nav-item ${screen === item.id ? "active" : ""}`} onClick={() => navigate(item.id)} aria-current={screen === item.id ? "page" : undefined}><Icon size={18} strokeWidth={1.8} /><span>{item.label}</span></button></div>; }) : <><p className="nav-section">Teaching</p><button className={`nav-item ${screen !== "connections" && screen !== "settings" ? "active" : ""}`} onClick={() => navigate("learn")}><LayoutDashboard size={18} /> Course workspace</button><p className="nav-section">System</p><button className={`nav-item ${screen === "connections" ? "active" : ""}`} onClick={() => navigate("connections")}><Cable size={18} /> Connections</button><button className={`nav-item ${screen === "settings" ? "active" : ""}`} onClick={() => navigate("settings")}><Settings size={18} /> Settings & privacy</button></>}
        </nav>
        <div className="rail-note"><ShieldCheck size={17} /><div><strong>{memory?.personalization_enabled === false ? "Personalization paused" : "Memory stays local"}</strong><span>Backend-verified state</span></div></div>
      </aside>
      {mobileOpen && <button className="nav-backdrop" onClick={() => setMobileOpen(false)} aria-label="Close navigation" />}
      <div className="app-body">
        <header className="top-bar">
          <button className="icon-button menu-button" onClick={() => setMobileOpen(true)} aria-label="Open navigation"><Menu size={21} /></button>
          <div className="context-title"><span className="eyebrow">{teacher ? "Teacher workspace" : "Learner workspace"}</span><span>{String(profile?.institution || (teacher ? "Course administration" : "Personal learning"))}</span></div>
          <div className="top-actions"><div className={`sync-state ${connection}`} aria-live="polite"><span /> {connection === "online" ? "Backend connected" : "Connection interrupted"}</div><div className="profile-wrap"><button className="profile-button" onClick={() => setProfileOpen((value) => !value)} aria-expanded={profileOpen}><span className="avatar">{initials}</span><span className="profile-copy"><strong>{displayName}</strong><small>{teacher ? "Teacher" : "Learner"}</small></span><ChevronDown size={16} /></button>{profileOpen && <div className="profile-menu" role="menu"><p>Signed in as {user.username}</p><button onClick={() => { setProfileOpen(false); void refresh(); }}><RefreshCw size={17} /> Refresh data</button><button onClick={() => { setProfileOpen(false); setScreen("learn"); void logout(); }}><X size={17} /> Sign out</button></div>}</div></div>
        </header>
        <main id="main-content" tabIndex={-1}>{teacher && screen !== "connections" && screen !== "settings" ? <TeacherWorkspace /> : <LearnerWorkspace screen={screen} onNavigate={navigate} learningLaunch={learningLaunch} />}</main>
        {error && <div className="app-error" role="alert"><span>{error}</span><button onClick={() => void refresh()}>Retry</button></div>}
      </div>
      {!teacher && <nav className="mobile-dock" aria-label="Mobile navigation">{[{ id: "learn" as const, label: "Learn", icon: BookOpen }, { id: "chat" as const, label: "Chat", icon: MessageSquareText }, { id: "graph" as const, label: "Graph", icon: Network }, { id: "gaps" as const, label: "Gaps", icon: Focus }].map((item) => { const Icon = item.icon; return <button key={item.id} className={screen === item.id ? "active" : ""} onClick={() => navigate(item.id)}><Icon size={20} /><span>{item.label}</span></button>; })}<button onClick={() => setMobileOpen(true)}><Menu size={20} /><span>More</span></button></nav>}
    </div>
  );
}
