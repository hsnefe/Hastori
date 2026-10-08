// Turkish interface: numbers as tr-TR, times in the site's time zone. Call these from client
// components that render data (never during server rendering of a page without data), so the
// server's own locale and zone cannot leak into the markup.

import type { Metric, RuleKind } from "./types";

export const LOCALE = "tr-TR";

export const METRICS: Record<Metric, { label: string; unit: string; digits: number }> = {
  active_power_kw: { label: "Etkin güç", unit: "kW", digits: 1 },
  reactive_power_kvar: { label: "Reaktif güç", unit: "kVAr", digits: 1 },
  current_a: { label: "Akım", unit: "A", digits: 1 },
  temperature_c: { label: "Sıcaklık", unit: "°C", digits: 1 },
};

export const METRIC_ORDER: Metric[] = ["active_power_kw", "reactive_power_kvar", "current_a", "temperature_c"];

export const DEVICE_TYPES: Record<string, string> = {
  compressor: "Kompresör",
  chiller: "Chiller",
  energy_analyzer: "Enerji analizörü",
};

export const SEVERITIES = { warning: "Uyarı", critical: "Kritik" } as const;
export const ALARM_STATES = { active: "Aktif", acknowledged: "Onaylandı", cleared: "Kapandı" } as const;
export const ROLES = { system_admin: "Sistem yöneticisi", site_admin: "Tesis yöneticisi", viewer: "İzleyici" } as const;

export function formatNumber(value: number, digits = 1): string {
  return new Intl.NumberFormat(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}

export function formatMetric(metric: Metric, value: number): string {
  const m = METRICS[metric];
  return `${formatNumber(value, m.digits)} ${m.unit}`;
}

/**
 * A rule's value: a measured quantity with its unit, a plain ratio (reactive_ratio rules) or, for
 * no_data rules, the length of the silence.
 */
export function formatRuleValue(kind: RuleKind, metric: Metric, value: number): string {
  if (kind === "no_data") return formatDuration(value);
  return kind === "reactive_ratio" ? formatNumber(value, 3) : formatMetric(metric, value);
}

export function formatPercent(fraction: number): string {
  return new Intl.NumberFormat(LOCALE, { style: "percent", maximumFractionDigits: 0 }).format(fraction);
}

export function formatTime(ms: number | string | Date, timeZone: string, seconds = true): string {
  return new Intl.DateTimeFormat(LOCALE, {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
    hour12: false,
  }).format(new Date(ms));
}

export function formatDateTime(ms: number | string | Date, timeZone: string): string {
  return new Intl.DateTimeFormat(LOCALE, {
    timeZone,
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(ms));
}

/** "2026-10-08" (a day in the site's zone, as the API sends it) -> "Per 08.10". */
export function formatDay(isoDate: string): string {
  const [y, m, d] = isoDate.split("-").map(Number) as [number, number, number];
  // Noon UTC: any zone reads the same calendar day.
  return new Intl.DateTimeFormat(LOCALE, { timeZone: "UTC", weekday: "short", day: "2-digit", month: "2-digit" }).format(
    new Date(Date.UTC(y, m - 1, d, 12)),
  );
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} sn`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} dk ${Math.round(seconds % 60)} sn`;
  return `${Math.floor(minutes / 60)} sa ${minutes % 60} dk`;
}

/** A short label for a time zone name ("Europe/Istanbul" -> "Istanbul"). */
export function zoneLabel(timeZone: string): string {
  return timeZone.split("/").pop()?.replace(/_/g, " ") ?? timeZone;
}
