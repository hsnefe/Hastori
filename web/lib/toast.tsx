"use client";

import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

export type ToastKind = "info" | "success" | "warning" | "critical";

interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}

const ToastContext = createContext<((kind: ToastKind, text: string) => void) | null>(null);

const SHOW_MS = 8000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);
  const paused = useRef(new Set<number>());

  const remove = useCallback((id: number) => setToasts((list) => list.filter((t) => t.id !== id)), []);

  const push = useCallback(
    (kind: ToastKind, text: string) => {
      const id = next.current++;
      setToasts((list) => [...list.slice(-3), { id, kind, text }]);
      if (kind === "critical") return; // a critical alarm stays until somebody dismisses it
      const tick = () => {
        // Hovering or focusing a message gives the reader time (WCAG 2.2.1).
        if (paused.current.has(id)) setTimeout(tick, 1000);
        else remove(id);
      };
      setTimeout(tick, SHOW_MS);
    },
    [remove],
  );

  const value = useMemo(() => push, [push]);
  const hold = (id: number) => paused.current.add(id);
  const release = (id: number) => paused.current.delete(id);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`toast toast-${t.kind}`}
            // A critical message interrupts; the rest wait for a pause in the screen reader.
            role={t.kind === "critical" ? "alert" : undefined}
            onMouseEnter={() => hold(t.id)}
            onMouseLeave={() => release(t.id)}
            onFocus={() => hold(t.id)}
            onBlur={() => release(t.id)}
          >
            <span>{t.text}</span>
            <button type="button" className="icon-button" aria-label="Kapat" onClick={() => remove(t.id)}>
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): (kind: ToastKind, text: string) => void {
  const push = useContext(ToastContext);
  if (!push) throw new Error("useToast outside ToastProvider");
  return push;
}
