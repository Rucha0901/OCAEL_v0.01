"use client";

import type { LearnerScreen, LearningLaunch } from "../../types";
import { CallScreen } from "./screens/CallScreen";
import { ConnectionsScreen } from "./screens/ConnectionsScreen";
import { GraphScreen } from "./screens/GraphScreen";
import { GapForensicsScreen } from "./screens/GapForensicsScreen";
import { HistoryScreen } from "./screens/HistoryScreen";
import { LearnScreen } from "./screens/LearnScreen";
import { MaterialsScreen } from "./screens/MaterialsScreen";
import { ChatScreen } from "./screens/ChatScreen";
import { SettingsScreen } from "./screens/SettingsScreen";

export function LearnerWorkspace({ screen, onNavigate, learningLaunch }: { screen: LearnerScreen; onNavigate: (screen: LearnerScreen, launch?: LearningLaunch) => void; learningLaunch: LearningLaunch | null }) {
  switch (screen) {
    case "call": return <CallScreen />;
    case "chat": return <ChatScreen />;
    case "graph": return <GraphScreen onLearn={(launch) => onNavigate("learn", launch)} />;
    case "gaps": return <GapForensicsScreen onLearn={(launch) => onNavigate("learn", launch)} />;
    case "materials": return <MaterialsScreen onLearn={(launch) => onNavigate("learn", launch)} />;
    case "history": return <HistoryScreen />;
    case "connections": return <ConnectionsScreen />;
    case "settings": return <SettingsScreen />;
    default: return <LearnScreen onNavigate={onNavigate} launch={learningLaunch} />;
  }
}
