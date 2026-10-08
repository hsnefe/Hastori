"use client";

// Who is signed in. The access token is in memory (lib/api.ts); on every page load the httpOnly
// refresh cookie is exchanged for a new one, so a reload keeps the session without storing a token.

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api } from "./api";
import { useToast } from "./toast";
import type { Me } from "./types";

type Status = "loading" | "anonymous" | "authenticated" | "unavailable";

interface SessionValue {
  status: Status;
  me: Me | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** After "unavailable": try to restore the session again. */
  retry: () => void;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>("loading");
  const [me, setMe] = useState<Me | null>(null);

  const [attempt, setAttempt] = useState(0);
  const toast = useToast();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const result = await api.restore();
        if (cancelled) return;
        if (result.kind === "unavailable") return setStatus("unavailable"); // not "signed out"
        const user = result.kind === "ok" ? await api.get<Me>("/auth/me") : null;
        if (cancelled) return;
        setMe(user);
        setStatus(user ? "authenticated" : "anonymous");
      } catch {
        if (!cancelled) setStatus("unavailable");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const retry = useCallback(() => {
    setStatus("loading");
    setAttempt((n) => n + 1);
  }, []);

  useEffect(
    () =>
      api.onSessionExpired(() => {
        queryClient.clear();
        setMe(null);
        setStatus("anonymous");
      }),
    [queryClient],
  );

  const login = useCallback(async (email: string, password: string) => {
    await api.login(email, password);
    const user = await api.get<Me>("/auth/me");
    setMe(user);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // The screen signs out either way, but the server did not hear it: the httpOnly cookie
      // stays valid until it expires. Say so, on a shared computer that matters.
      toast("warning", "Çıkış sunucuya iletilemedi; bu tarayıcıda oturum bir süre açık kalabilir.");
    }
    queryClient.clear();
    setMe(null);
    setStatus("anonymous");
  }, [queryClient, toast]);

  const value = useMemo(() => ({ status, me, login, logout, retry }), [status, me, login, logout, retry]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}

/** Roles that may acknowledge alarms and change rules (the API enforces it as well). */
export function canWrite(me: Me | null): boolean {
  return me?.role === "system_admin" || me?.role === "site_admin";
}
