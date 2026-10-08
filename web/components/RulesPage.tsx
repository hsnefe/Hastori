"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { api, ApiError } from "@/lib/api";
import { formatNumber, METRICS, SEVERITIES } from "@/lib/format";
import { parseDecimal, parseInteger } from "@/lib/numbers";
import { canWrite, useSession } from "@/lib/session";
import { useToast } from "@/lib/toast";
import type { Page, Rule, RuleBody } from "@/lib/types";

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

export function RulesPage({ siteId }: { siteId: string }) {
  const { me } = useSession();
  const editable = canWrite(me);
  const rules = useQuery({
    queryKey: ["rules", siteId],
    queryFn: () => api.get<Page<Rule>>(`/alarm-rules?site_id=${siteId}&size=100`),
  });
  const [editing, setEditing] = useState<string | null>(null);

  return (
    <section className="card" aria-labelledby="rules-title">
      <div className="card-head">
        <h2 id="rules-title">Alarm kuralları</h2>
      </div>
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

function condition(rule: Rule): string {
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
      <td>{formatNumber(rule.clear_threshold, rule.kind === "reactive_ratio" ? 3 : 1)}</td>
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
    const t = parseDecimal(threshold);
    const c = parseDecimal(clear);
    const d = parseInteger(duration);
    if (t === null || c === null) return setError("Eşikler sayı olmalı (örnek: 80 ya da 0,18; binlik ayracı yok).");
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
      <td>
        {rule.kind === "reactive_ratio" ? "Reaktif oran" : METRICS[rule.metric].label} {rule.operator}{" "}
        <input form={form} aria-label="Eşik" inputMode="decimal" value={threshold} onChange={(e) => setThreshold(e.target.value)} size={7} />
      </td>
      <td>
        <input form={form} aria-label="Kapanma eşiği" inputMode="decimal" value={clear} onChange={(e) => setClear(e.target.value)} size={7} />
      </td>
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
