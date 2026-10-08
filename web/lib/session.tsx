"use client";

// Who is signed in. The access token is in memory (lib/api.ts); on every page load the httpOnly
// refresh cookie is exchanged for a new one, so a reload keeps the session without storing a token.

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api } from "./api";
import type { Me } from "./types";

type Status = "loading" | "anonymous" | "authenticated";

interface SessionValue {
  status: Status;
  me: Me | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>("loading");
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const token = await api.restore();
        const user = token ? await api.get<Me>("/auth/me") : null;
        if (cancelled) return;
        setMe(user);
        setStatus(user ? "authenticated" : "anonymous");
      } catch {
        if (!cancelled) setStatus("anonymous");
      }
    })();
    return () => {
      cancelled = true;
    };
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
      // the cookie may already be gone; the screen signs out either way
    }
    queryClient.clear();
    setMe(null);
    setStatus("anonymous");
  }, [queryClient]);

  const value = useMemo(() => ({ status, me, login, logout }), [status, me, login, logout]);
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
