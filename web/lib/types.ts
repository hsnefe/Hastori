// The shapes of the REST API (services/api/src/hastori_api/schemas.py) and of the WebSocket.

export type Role = "system_admin" | "site_admin" | "viewer";
export type Severity = "warning" | "critical";
export type AlarmState = "active" | "acknowledged" | "cleared";
export type Metric = "active_power_kw" | "reactive_power_kvar" | "current_a" | "temperature_c";

export interface Site {
  id: string;
  name: string;
  city: string | null;
  timezone: string;
}

export interface Me {
  id: string;
  email: string;
  role: Role;
  org_id: string;
  sites: Site[];
}

export interface TokenOut {
  access_token: string;
  expires_in: number;
}

export interface LatestValue {
  value: number;
  time: string;
}

export interface Device {
  id: string;
  site_id: string;
  key: string;
  name: string;
  type: string;
  is_active: boolean;
  online: boolean;
  last_seen: string | null;
  latest: Partial<Record<Metric, LatestValue>>;
}

export interface SeriesPoint {
  time: string;
  value: number;
}

export interface Series {
  device_id: string;
  metric: Metric;
  interval: "raw" | "1m" | "1h";
  start: string;
  end: string;
  points: SeriesPoint[];
}

export interface DayConsumption {
  date: string;
  kwh: number;
  coverage: number;
  reactive_kvarh: number;
  reactive_ratio: number | null;
}

export interface Consumption {
  site_id: string;
  timezone: string;
  days: DayConsumption[];
}

export interface Alarm {
  id: string;
  state: AlarmState;
  severity: Severity;
  rule_id: string;
  rule_name: string;
  metric: Metric;
  device_id: string;
  device_name: string;
  site_id: string;
  site_name: string;
  opened_at: string;
  acked_at: string | null;
  acked_by: string | null;
  /** Who acknowledged: a name, never an e-mail address. */
  acked_by_label: string | null;
  cleared_at: string | null;
  peak_value: number | null;
}

export interface TimelineEntry {
  event: "opened" | "acknowledged" | "cleared";
  at: string;
  by: string | null;
}

/** no_data: the metric stopped arriving for `duration_s` seconds (no thresholds). */
export type RuleKind = "threshold" | "reactive_ratio" | "no_data";

export interface Rule {
  id: string;
  device_id: string;
  device_name: string;
  site_id: string;
  name: string;
  kind: RuleKind;
  metric: Metric;
  operator: ">" | "<";
  threshold: number;
  clear_threshold: number;
  duration_s: number;
  window_s: number | null;
  severity: Severity;
  enabled: boolean;
}

export type RuleBody = Omit<Rule, "id" | "device_id" | "device_name" | "site_id">;

export interface AlarmDetail extends Alarm {
  threshold: number | null;
  clear_threshold: number | null;
  timeline: TimelineEntry[];
  rule: Rule;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

// -- WebSocket ------------------------------------------------------------------------------

export interface MeasurementEvent {
  type: "measurement";
  device_id: string;
  ts: number; // epoch seconds, the device's clock
  metrics: Partial<Record<Metric, number>>;
}

export interface AlarmEvent {
  type: "alarm.opened" | "alarm.acknowledged" | "alarm.cleared";
  alarm_id: string;
  rule_id: string;
  rule_name: string;
  device_id: string;
  site_id: string;
  severity: Severity;
  state: AlarmState;
  value: number | null;
  ts: number;
}

export type LiveMessage =
  | MeasurementEvent
  | AlarmEvent
  | { type: "hello"; sites: string[]; ts: number }
  | { type: "resync" }
  | { type: "hb" }
  | { type: "error"; code: string; message: string };
