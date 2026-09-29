/**
 * How much of the model's hidden reasoning ("thinking") the UI shows.
 *
 * DISPLAY ONLY. The backend always streams whatever reasoning the provider
 * returns and persists it on the message (``meta.reasoning``); this setting
 * decides how much of that text is rendered. It never changes the request —
 * the reasoning-effort pill next to it is the one that does.
 *
 *   off   — nothing rendered.
 *   brief — one muted line: the tail of the reasoning (the latest thought
 *           while streaming), expandable per message.
 *   full  — the whole reasoning in a collapsible block, open by default.
 *
 * Kept as a React context (not a prop) because the consumer is the memoized
 * MessageBubble deep inside Thread; context updates re-render through
 * ``memo`` without threading a prop past every bubble comparator.
 */
import { createContext } from "react";

export type ThinkingMode = "off" | "brief" | "full";

export const THINKING_MODES: readonly ThinkingMode[] = ["off", "brief", "full"];

export const THINKING_PREF_KEY = "chat.thinking.v1";

export const DEFAULT_THINKING_MODE: ThinkingMode = "brief";

export function isThinkingMode(v: unknown): v is ThinkingMode {
  return v === "off" || v === "brief" || v === "full";
}

export function loadThinkingMode(): ThinkingMode {
  try {
    const raw = localStorage.getItem(THINKING_PREF_KEY);
    return isThinkingMode(raw) ? raw : DEFAULT_THINKING_MODE;
  } catch {
    return DEFAULT_THINKING_MODE;
  }
}

export function saveThinkingMode(mode: ThinkingMode): void {
  try {
    localStorage.setItem(THINKING_PREF_KEY, mode);
  } catch {
    /* localStorage unavailable — memory-only is fine */
  }
}

/** Characters of reasoning shown in "brief" mode (the tail). */
export const BRIEF_TAIL_CHARS = 160;

export const ThinkingModeContext = createContext<ThinkingMode>(DEFAULT_THINKING_MODE);
