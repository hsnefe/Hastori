"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { api, ApiError } from "@/lib/api";
import { useDevices } from "@/lib/hooks";
import { formatNumber, METRIC_ORDER, METRICS, SEVERITIES } from "@/lib/format";
import { parseDecimal, parseInteger } from "@/lib/numbers";
import { canWrite, useSession } from "@/lib/session";
import { useToast } from "@/lib/toast";
import type { Metric, Page, Rule, RuleBody, RuleKind, Severity } from "@/lib/types";

export function ruleBody(rule: Rule, over: Partial<RuleBody>): RuleBody {
  return {
    name: rule.name,
    kind: rule.kind,
    metric: rule.metric,
    operator: rule.operator,
    threshold: rule.threshold,
    clear_threshold: rule.clear_threshold,
    duration_s: rule.duration_s,
    window_s: rule.window_s,
    severity: rule.severity,
    enabled: rule.enabled,
    ...over,
  };
}

/** The rule was changed by someone else between opening the form and saving it. */
export class RuleChangedError extends Error {
  constructor() {
    super("rule changed");
    this.name = "RuleChangedError";
  }
}

/** What the API said was wrong with a rule, in one sentence. */
export function ruleError(error: unknown): string {
  if (error instanceof RuleChangedError) return "Kural bu arada başka biri tarafından değiştirildi; liste yenilendi, tekrar deneyin.";
  if (error instanceof ApiError) {
    if (error.status === 422) return `Değerler geçersiz: ${error.message}`;
    if (error.status === 409) return "Bu cihaz ve ölçüm için etkin başka bir kural var.";
    if (error.status === 403) return "Kural değiştirme yetkiniz yok.";
    if (error.status === 404) return "Kural bulunamadı.";
  }
  return "Kural kaydedilemedi. Tekrar deneyin.";
}

/** What the "new rule" form holds: everything as typed. */
export interface NewRuleForm {
  name: string;
  deviceId: string;
  kind: RuleKind;
  metric: Metric;
  operator: ">" | "<";
  threshold: string;
  clear: string;
  duration: string;
  windowMin: string;
  severity: Severity;
}

export const EMPTY_RULE_FORM: NewRuleForm = {
  name: "",
  deviceId: "",
  kind: "threshold",
  metric: "temperature_c",
  operator: ">",
  threshold: "",
  clear: "",
  duration: "30",
  windowMin: "15",
  severity: "warning",
};

export const KIND_LABELS: Record<RuleKind, string> = {
  threshold: "Eşik (değer aşılırsa)",
  reactive_ratio: "Reaktif oran",
  no_data: "Veri gelmezse",
};

/** The body for POST /alarm-rules from the form, or the sentence that says what is wrong. */
export function newRuleBody(f: NewRuleForm): { body: RuleBody & { device_id: string } } | { error: string } {
  const name = f.name.trim();
  if (!name) return { error: "Kurala bir ad verin." };
  if (name.length > 120) return { error: "Ad en fazla 120 karakter olabilir." };
  if (!f.deviceId) return { error: "Bir cihaz seçin." };
  const duration = parseInteger(f.duration);
  const base = { name, device_id: f.deviceId, kind: f.kind, severity: f.severity, enabled: true };

  if (f.kind === "no_data") {
    if (duration === null || duration < 10 || duration > 600) return { error: "Süre 10 ile 600 sn arasında tam sayı olmalı." };
    return {
      body: { ...base, metric: f.metric, operator: ">", threshold: 0, clear_threshold: 0, duration_s: duration, window_s: null },
    };
  }
  const threshold = parseDecimal(f.threshold);
  const clear = parseDecimal(f.clear);
  if (threshold === null || clear === null) return { error: "Eşikler sayı olmalı (örnek: 80 ya da 0,18; binlik ayracı yok)." };
  if (duration === null || duration > 600) return { error: "Süre 0 ile 600 sn arasında tam sayı olmalı." };
  const operator = f.kind === "reactive_ratio" ? ">" : f.operator;
  if (operator === ">" && clear > threshold) return { error: "Kapanma eşiği eşikten büyük olamaz." };
  if (operator === "<" && clear < threshold) return { error: "Kapanma eşiği eşikten küçük olamaz." };
  let windowS: number | null = null;
  if (f.kind === "reactive_ratio") {
    const minutes = parseInteger(f.windowMin);
    if (minutes === null || minutes < 1 || minutes > 60) return { error: "Pencere 1 ile 60 dk arasında tam sayı olmalı." };
    windowS = minutes * 60;
  }
  return {
    body: {
      ...base,
      metric: f.kind === "reactive_ratio" ? "reactive_power_kvar" : f.metric,
      operator,
      threshold,
      clear_threshold: clear,
      duration_s: duration,
      window_s: windowS,
    },
  };
}

export function RulesPage({ siteId }: { siteId: string }) {
  const { me } = useSession();
  const editable = canWrite(me);
  const rules = useQuery({
    queryKey: ["rules", siteId],
    queryFn: () => api.get<Page<Rule>>(`/alarm-rules?site_id=${siteId}&size=100`),
  });
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  return (
    <section className="card" aria-labelledby="rules-title">
      <div className="card-head">
        <h2 id="rules-title">Alarm kuralları</h2>
        {editable && !creating ? (
          <button type="button" onClick={() => setCreating(true)}>
            Yeni kural
          </button>
        ) : null}
      </div>
      {editable && creating ? <RuleCreator siteId={siteId} onDone={() => setCreating(false)} /> : null}
      {!editable ? <p className="muted">Kuralları yalnızca tesis ve sistem yöneticileri değiştirebilir.</p> : null}
      {rules.isPending ? <p className="muted">Yükleniyor…</p> : null}
      {rules.isError ? <p className="form-error">Kurallar alınamadı.</p> : null}
      {rules.data ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Kural</th>
                <th>Cihaz</th>
                <th>Koşul</th>
                <th>Kapanma eşiği</th>
                <th>Süre</th>
                <th>Önem</th>
                <th>Durum</th>
                {editable ? <th /> : null}
              </tr>
            </thead>
            <tbody>
              {rules.data.items.map((rule) =>
                editing === rule.id ? (
                  <RuleEditor key={rule.id} rule={rule} siteId={siteId} onDone={() => setEditing(null)} />
                ) : (
                  <RuleRow key={rule.id} rule={rule} siteId={siteId} editable={editable} onEdit={() => setEditing(rule.id)} />
                ),
              )}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function RuleCreator({ siteId, onDone }: { siteId: string; onDone: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const devices = useDevices(siteId);
  const [form, setForm] = useState<NewRuleForm>(EMPTY_RULE_FORM);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof NewRuleForm>(key: K, value: NewRuleForm[K]) => setForm((f) => ({ ...f, [key]: value }));

  const create = useMutation({
    mutationFn: (body: RuleBody & { device_id: string }) => api.post<Rule>("/alarm-rules", body),
    onSuccess: () => {
      toast("success", "Kural oluşturuldu.");
      void queryClient.invalidateQueries({ queryKey: ["rules", siteId] });
      onDone();
    },
    onError: (e) => setError(ruleError(e)),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const result = newRuleBody(form);
    if ("error" in result) return setError(result.error);
    setError(null);
    create.mutate(result.body);
  }

  const list = (devices.data ?? []).filter((d) => d.is_active);
  const ratio = form.kind === "reactive_ratio";
  const silence = form.kind === "no_data";
  return (
    <form className="rule-new" onSubmit={submit} aria-label="Yeni alarm kuralı">
      <div className="fields">
        <label>
          Ad
          <input value={form.name} onChange={(e) => set("name", e.target.value)} maxLength={120} required />
        </label>
        <label>
          Cihaz
          <select value={form.deviceId} onChange={(e) => set("deviceId", e.target.value)} required>
            <option value="">Seçin…</option>
            {list.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Tür
          <select value={form.kind} onChange={(e) => set("kind", e.target.value as RuleKind)}>
            {(Object.keys(KIND_LABELS) as RuleKind[]).map((k) => (
              <option key={k} value={k}>
                {KIND_LABELS[k]}
              </option>
            ))}
          </select>
        </label>
        {!ratio ? (
          <label>
            Ölçüm
            <select value={form.metric} onChange={(e) => set("metric", e.target.value as Metric)}>
              {METRIC_ORDER.map((m) => (
                <option key={m} value={m}>
                  {METRICS[m].label} ({METRICS[m].unit})
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {!silence && !ratio ? (
          <label>
            Koşul
            <select value={form.operator} onChange={(e) => set("operator", e.target.value as ">" | "<")}>
              <option value=">">Büyükse (&gt;)</option>
              <option value="<">Küçükse (&lt;)</option>
            </select>
          </label>
        ) : null}
        {!silence ? (
          <>
            <label>
              Eşik
              <input inputMode="decimal" value={form.threshold} onChange={(e) => set("threshold", e.target.value)} required />
            </label>
            <label>
              Kapanma eşiği
              <input inputMode="decimal" value={form.clear} onChange={(e) => set("clear", e.target.value)} required />
            </label>
          </>
        ) : null}
        <label>
          {silence ? "Veri gelmeme süresi (sn)" : "Süre (sn)"}
          <input inputMode="numeric" value={form.duration} onChange={(e) => set("duration", e.target.value)} required />
        </label>
        {ratio ? (
          <label>
            Pencere (dk)
            <input inputMode="numeric" value={form.windowMin} onChange={(e) => set("windowMin", e.target.value)} required />
          </label>
        ) : null}
        <label>
          Önem
          <select value={form.severity} onChange={(e) => set("severity", e.target.value as Severity)}>
            <option value="warning">{SEVERITIES.warning}</option>
            <option value="critical">{SEVERITIES.critical}</option>
          </select>
        </label>
      </div>
      {ratio ? <p className="muted">Reaktif oran yalnızca ana pano analizöründe anlamlıdır; eşik 0,18 gibi bir orandır.</p> : null}
      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="row-actions">
        <button type="submit" className="primary" disabled={create.isPending}>
          {create.isPending ? "Oluşturuluyor…" : "Oluştur"}
        </button>
        <button type="button" onClick={onDone}>
          Vazgeç
        </button>
      </div>
    </form>
  );
}

function condition(rule: Rule): string {
  if (rule.kind === "no_data") return `${METRICS[rule.metric].label} verisi ${rule.duration_s} sn gelmezse`;
  const unit = rule.kind === "reactive_ratio" ? "" : ` ${METRICS[rule.metric].unit}`;
  const base = `${METRICS[rule.metric].label} ${rule.operator} ${formatNumber(rule.threshold, rule.kind === "reactive_ratio" ? 3 : 1)}${unit}`;
  return rule.kind === "reactive_ratio" ? `Reaktif oran ${rule.operator} ${formatNumber(rule.threshold, 3)} (${Math.round((rule.window_s ?? 0) / 60)} dk)` : base;
}

function RuleRow({ rule, siteId, editable, onEdit }: { rule: Rule; siteId: string; editable: boolean; onEdit: () => void }) {
  const save = useSaveRule(siteId);
  return (
    <tr className={rule.enabled ? undefined : "disabled"}>
      <td>{rule.name}</td>
      <td>{rule.device_name}</td>
      <td>{condition(rule)}</td>
      <td>{rule.kind === "no_data" ? "veri gelince" : formatNumber(rule.clear_threshold, rule.kind === "reactive_ratio" ? 3 : 1)}</td>
      <td>{rule.duration_s} sn</td>
      <td>
        <span className={`sev sev-${rule.severity}`}>{SEVERITIES[rule.severity]}</span>
      </td>
      <td>{rule.enabled ? "Etkin" : "Devre dışı"}</td>
      {editable ? (
        <td>
          <div className="row-actions">
            <button type="button" onClick={onEdit}>
              Düzenle
            </button>
            <button type="button" disabled={save.isPending} onClick={() => save.mutate({ rule, body: ruleBody(rule, { enabled: !rule.enabled }) })}>
              {rule.enabled ? "Devre dışı bırak" : "Etkinleştir"}
            </button>
          </div>
        </td>
      ) : null}
    </tr>
  );
}

function useSaveRule(siteId: string, onSaved?: () => void) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: async ({ rule, body }: { rule: Rule; body: RuleBody }) => {
      // The whole rule is sent, so a stale form would silently undo someone else's change (or
      // switch a rule back on). Compare with what is stored now before overwriting it.
      const current = await api.get<Rule>(`/alarm-rules/${encodeURIComponent(rule.id)}`);
      if (JSON.stringify(ruleBody(current, {})) !== JSON.stringify(ruleBody(rule, {}))) {
        throw new RuleChangedError();
      }
      return api.put<Rule>(`/alarm-rules/${encodeURIComponent(rule.id)}`, body);
    },
    onSuccess: () => {
      toast("success", "Kural kaydedildi.");
      void queryClient.invalidateQueries({ queryKey: ["rules", siteId] });
      onSaved?.();
    },
    onError: (error) => {
      toast("warning", ruleError(error));
      if (error instanceof RuleChangedError) void queryClient.invalidateQueries({ queryKey: ["rules", siteId] });
    },
  });
}

function RuleEditor({ rule, siteId, onDone }: { rule: Rule; siteId: string; onDone: () => void }) {
  const save = useSaveRule(siteId, onDone);
  const [threshold, setThreshold] = useState(String(rule.threshold));
  const [clear, setClear] = useState(String(rule.clear_threshold));
  const [duration, setDuration] = useState(String(rule.duration_s));
  const [error, setError] = useState<string | null>(null);

  function submit(event: FormEvent) {
    event.preventDefault();
    const silence = rule.kind === "no_data"; // no thresholds: only the time limit is edited
    const t = silence ? 0 : parseDecimal(threshold);
    const c = silence ? 0 : parseDecimal(clear);
    const d = parseInteger(duration);
    if (t === null || c === null) return setError("Eşikler sayı olmalı (örnek: 80 ya da 0,18; binlik ayracı yok).");
    if (silence && (d === null || d < 10 || d > 600)) return setError("Süre 10 ile 600 sn arasında tam sayı olmalı.");
    if (d === null || d > 600) return setError("Süre 0 ile 600 sn arasında tam sayı olmalı.");
    if (rule.operator === ">" && c > t) return setError("Kapanma eşiği eşikten büyük olamaz.");
    if (rule.operator === "<" && c < t) return setError("Kapanma eşiği eşikten küçük olamaz.");
    setError(null);
    save.mutate({ rule, body: ruleBody(rule, { threshold: t, clear_threshold: c, duration_s: d }) });
  }

  const form = `rule-${rule.id}`;
  return (
    <tr className="editing">
      <td>{rule.name}</td>
      <td>{rule.device_name}</td>
      {rule.kind === "no_data" ? (
        <>
          <td>{METRICS[rule.metric].label} verisi gelmezse</td>
          <td>veri gelince</td>
        </>
      ) : (
        <>
          <td>
            {rule.kind === "reactive_ratio" ? "Reaktif oran" : METRICS[rule.metric].label} {rule.operator}{" "}
            <input form={form} aria-label="Eşik" inputMode="decimal" value={threshold} onChange={(e) => setThreshold(e.target.value)} size={7} />
          </td>
          <td>
            <input form={form} aria-label="Kapanma eşiği" inputMode="decimal" value={clear} onChange={(e) => setClear(e.target.value)} size={7} />
          </td>
        </>
      )}
      <td>
        <input form={form} aria-label="Süre (sn)" inputMode="numeric" value={duration} onChange={(e) => setDuration(e.target.value)} size={4} /> sn
      </td>
      <td>
        <span className={`sev sev-${rule.severity}`}>{SEVERITIES[rule.severity]}</span>
      </td>
      <td>{rule.enabled ? "Etkin" : "Devre dışı"}</td>
      <td>
        <form id={form} onSubmit={submit} />
        <div className="row-actions">
          <button type="submit" form={form} className="primary" disabled={save.isPending}>
            Kaydet
          </button>
          <button type="button" onClick={onDone}>
            Vazgeç
          </button>
          {error ? (
            <span className="form-error" role="alert">
              {error}
            </span>
          ) : null}
        </div>
      </td>
    </tr>
  );
}
