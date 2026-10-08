"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api } from "./api";
import { useLive, type LiveDevice } from "./live";
import { mergeLive, trim, WINDOW_MS, type Point } from "./series";
import type { Consumption, Metric, Series } from "./types";

/** A device is online while a reading arrived in the last 30 seconds (the API's definition). */
export const ONLINE_WITHIN_MS = 30_000;
export const SAFETY_REFRESH_MS = 60_000;

/** Re-renders every `ms`; "online" and "5 sn önce" must change without any event arriving. */
export function useNow(ms: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

export function useDevices(siteId: string) {
  return useQuery({
    queryKey: ["devices", siteId],
    queryFn: () => api.get<LiveDevice[]>(`/sites/${siteId}/devices`),
    refetchInterval: SAFETY_REFRESH_MS,
  });
}

export function isOnline(device: LiveDevice, now: number): boolean {
  // Live: when the last reading reached this screen. Before any arrived: the API's answer.
  return device.arrivedAt !== undefined ? now - device.arrivedAt <= ONLINE_WITHIN_MS : device.online;
}

export function useConsumption(siteId: string) {
  return useQuery({
    queryKey: ["consumption", siteId],
    queryFn: () => api.get<Consumption>(`/sites/${siteId}/consumption/daily?days=7`),
    refetchInterval: SAFETY_REFRESH_MS,
  });
}

const REDRAW_MS = 1000;

/**
 * The last 15 minutes of one metric of one device: one REST read, then the socket appends.
 * Redraws are limited to once a second whatever the arrival rate, so the chart stays cheap.
 */
export function useLiveSeries(deviceId: string | undefined, metric: Metric): { points: Point[]; loading: boolean } {
  const { subscribe } = useLive();
  const base = useQuery({
    queryKey: ["series", deviceId, metric],
    enabled: deviceId !== undefined,
    staleTime: Infinity, // only a resync (invalidation) reads it again
    queryFn: () => {
      const to = new Date();
      const from = new Date(to.getTime() - WINDOW_MS);
      return api.get<Series>(
        `/devices/${deviceId}/measurements?metric=${metric}&interval=raw&from=${from.toISOString()}&to=${to.toISOString()}`,
      );
    },
  });
  const [points, setPoints] = useState<Point[]>([]);
  const live = useRef<Point[]>([]);
  const dirty = useRef(false);
  const snapshot = base.data;

  useEffect(() => {
    live.current = [];
    dirty.current = true;
    if (!deviceId) return;
    return subscribe((event) => {
      const value = event.metrics[metric];
      if (event.device_id !== deviceId || typeof value !== "number") return;
      const t = event.ts * 1000;
      const last = live.current[live.current.length - 1];
      if (last && t <= last.t) return;
      live.current.push({ t, v: value });
      dirty.current = true;
    });
  }, [deviceId, metric, subscribe]);

  useEffect(() => {
    dirty.current = true; // a new snapshot (first read, or after a resync)
  }, [snapshot]);

  useEffect(() => {
    const redraw = () => {
      if (!dirty.current) return;
      dirty.current = false;
      const basePoints: Point[] = (snapshot?.points ?? []).map((p) => ({ t: Date.parse(p.time), v: p.value }));
      const now = Date.now();
      live.current = trim(live.current, now);
      setPoints(trim(mergeLive(basePoints, live.current), now));
    };
    redraw();
    const id = setInterval(redraw, REDRAW_MS);
    return () => clearInterval(id);
  }, [snapshot]);

  return { points, loading: base.isPending && deviceId !== undefined };
}
