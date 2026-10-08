"use client";

// The live connection of the site on screen, and what its messages do:
//   measurement -> the device cards (patched in place) and the chart (through `subscribe`)
//   alarm.*     -> ask REST again; a fresh alarm also gets a notification
//   resync      -> ask REST for everything (first connect, or the server's Redis link came back)

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { api } from "./api";
import { SEVERITIES } from "./format";
import { useToast } from "./toast";
import type { Device, LiveMessage, MeasurementEvent } from "./types";
import { LiveConnection, type ConnectionState, type SocketLike } from "./ws";

type Listener = (event: MeasurementEvent) => void;

interface LiveValue {
  state: ConnectionState;
  /** Measurement events of the site, as they arrive. */
  subscribe: (listener: Listener) => () => void;
}

const LiveContext = createContext<LiveValue | null>(null);

/** A notification is for an alarm that just opened, not for one a reconnect replays. */
const FRESH_ALARM_S = 60;

export type LiveDevice = Device & { arrivedAt?: number };

export function wsUrl(ticket: string, env = process.env.NEXT_PUBLIC_WS_URL): string {
  const base =
    env && env.length > 0
      ? env
      : `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/api/v1/ws`;
  return `${base}?ticket=${encodeURIComponent(ticket)}`;
}

/** A device card shows the newest values; this is the cache patch one measurement makes. */
export function applyMeasurement(devices: LiveDevice[] | undefined, event: MeasurementEvent, arrivedAt: number): LiveDevice[] | undefined {
  if (!devices) return devices;
  const time = new Date(event.ts * 1000).toISOString();
  return devices.map((d) => {
    if (d.id !== event.device_id) return d;
    const latest = { ...d.latest };
    for (const [metric, value] of Object.entries(event.metrics)) {
      if (typeof value === "number") latest[metric as keyof typeof latest] = { value, time };
    }
    return { ...d, latest, online: true, last_seen: time, arrivedAt };
  });
}

export function LiveProvider({ siteId, children }: { siteId: string; children: ReactNode }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [state, setState] = useState<ConnectionState>("connecting");
  const listeners = useRef(new Set<Listener>());

  const handle = useCallback(
    (message: LiveMessage) => {
      switch (message.type) {
        case "measurement":
          queryClient.setQueryData<LiveDevice[]>(["devices", siteId], (old) => applyMeasurement(old, message, Date.now()));
          listeners.current.forEach((fn) => fn(message));
          break;
        case "alarm.opened":
        case "alarm.acknowledged":
        case "alarm.cleared":
          void queryClient.invalidateQueries({ queryKey: ["alarms"] });
          void queryClient.invalidateQueries({ queryKey: ["alarm", message.alarm_id] });
          if (message.type === "alarm.opened" && Date.now() / 1000 - message.ts <= FRESH_ALARM_S) {
            toast(message.severity === "critical" ? "critical" : "warning", `${SEVERITIES[message.severity]} alarm: ${message.rule_name}`);
          }
          break;
        case "resync":
          // Everything is stale; an open series snapshot included (its live tail restarts too).
          void queryClient.invalidateQueries();
          break;
        default:
          break;
      }
    },
    [queryClient, siteId, toast],
  );

  useEffect(() => {
    const connection = new LiveConnection({
      siteId,
      getTicket: async () => (await api.post<{ ticket: string }>("/ws-ticket")).ticket,
      // The browser WebSocket has the handlers and methods SocketLike names; its event types are wider.
      makeSocket: (ticket) => new WebSocket(wsUrl(ticket)) as unknown as SocketLike,
      onMessage: handle,
      onState: setState,
    });
    connection.start();
    // Back from sleep or from a network break: reconnect now, not after the rest of a back-off.
    const nudge = () => {
      if (document.visibilityState === "visible") connection.nudge();
    };
    window.addEventListener("online", nudge);
    document.addEventListener("visibilitychange", nudge);
    return () => {
      window.removeEventListener("online", nudge);
      document.removeEventListener("visibilitychange", nudge);
      connection.stop();
    };
  }, [siteId, handle]);

  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const value = useMemo(() => ({ state, subscribe }), [state, subscribe]);
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveValue {
  const value = useContext(LiveContext);
  if (!value) throw new Error("useLive outside LiveProvider");
  return value;
}
