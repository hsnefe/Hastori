"use client";

import { useEffect, useSyncExternalStore } from "react";

// Light, dark, or follow the system. The choice is a per-browser convenience; the page renders
// fine without storage.
type Choice = "system" | "light" | "dark";
const KEY = "hastori-theme";
const listeners = new Set<() => void>();

function read(): Choice {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

function apply(choice: Choice) {
  if (choice === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", choice);
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

const LABEL: Record<Choice, string> = { system: "Sistem teması", light: "Açık tema", dark: "Koyu tema" };
const NEXT: Record<Choice, Choice> = { system: "light", light: "dark", dark: "system" };
const ICON: Record<Choice, string> = { system: "◐", light: "☀", dark: "☾" };

export function ThemeToggle() {
  const choice = useSyncExternalStore(subscribe, read, () => "system" as Choice);
  // The stored choice wins after a reload.
  useEffect(() => apply(choice), [choice]);
  return (
    <button
      type="button"
      className="icon-button"
      title={`${LABEL[choice]} (değiştir)`}
      aria-label={`${LABEL[choice]}, değiştirmek için tıklayın`}
      onClick={() => {
        const next = NEXT[choice];
        try {
          if (next === "system") localStorage.removeItem(KEY);
          else localStorage.setItem(KEY, next);
        } catch {
          // storage is blocked: the choice lasts until reload
        }
        apply(next);
        listeners.forEach((fn) => fn());
      }}
    >
      {ICON[choice]}
    </button>
  );
}
