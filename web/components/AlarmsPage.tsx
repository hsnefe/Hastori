"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { HISTORY_PAGE_SIZE, useAcknowledge, useAlarmDetail, useAlarmHistory, type HistoryFilter } from "@/lib/alarms";
import {
  ALARM_STATES,
  formatDateTime,
  formatDuration,
  formatRuleValue,
  METRICS,
  SEVERITIES,
  zoneLabel,
} from "@/lib/format";
import { canWrite, useSession } from "@/lib/session";
import type { AlarmState } from "@/lib/types";
import { startOfDay, startOfNextDay } from "@/lib/zone";

import { ActiveAlarms } from "./ActiveAlarms";

export function AlarmsPage({ siteId }: { siteId: string }) {
  const { me } = useSession();
  const params = useSearchParams();
  const router = useRouter();
  const site = me?.sites.find((s) => s.id === siteId);
  const timeZone = site?.timezone ?? "Europe/Istanbul";
  const detailId = params.get("alarm");
  const [filter, setFilter] = useState<HistoryFilter>({ state: "", from: "", to: "", page: 1 });

  const bounds = {
    from: filter.from ? new Date(startOfDay(filter.from, timeZone)).toISOString() : undefined,
    to: filter.to ? new Date(startOfNextDay(filter.to, timeZone)).toISOString() : undefined,
  };
  const history = useAlarmHistory(siteId, filter, bounds);
  const pages = history.data ? Math.max(1, Math.ceil(history.data.total / HISTORY_PAGE_SIZE)) : 1;

  const change = (patch: Partial<HistoryFilter>) => setFilter((f) => ({ ...f, page: 1, ...patch }));

  return (
    <div className="stack">
      <ActiveAlarms siteId={siteId} />

      <section className="card" aria-labelledby="history-title">
        <div className="card-head">
          <h2 id="history-title">Alarm geçmişi</h2>
          <div className="controls">
            <label>
              Durum
              <select value={filter.state} onChange={(e) => change({ state: e.target.value as AlarmState | "" })}>
                <option value="">Tümü</option>
                {(Object.keys(ALARM_STATES) as AlarmState[]).map((s) => (
                  <option key={s} value={s}>
                    {ALARM_STATES[s]}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Başlangıç
              <input type="date" value={filter.from} max={filter.to || undefined} onChange={(e) => change({ from: e.target.value })} />
            </label>
            <label>
              Bitiş
              <input type="date" value={filter.to} min={filter.from || undefined} onChange={(e) => change({ to: e.target.value })} />
            </label>
          </div>
        </div>
        <p className="muted">Saatler {zoneLabel(timeZone)} saat dilimindedir.</p>
        {history.isPending ? <p className="muted">Yükleniyor…</p> : null}
        {history.isError ? (
          <p className="form-error" role="alert">
            Geçmiş alınamadı.{" "}
            <button type="button" className="link" onClick={() => void history.refetch()}>
              Tekrar dene
            </button>
          </p>
        ) : null}
        {history.data && history.data.items.length === 0 ? <p className="muted">Bu süzgeçle alarm yok.</p> : null}
        {history.data && history.data.items.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Açıldı</th>
                  <th>Önem</th>
                  <th>Kural</th>
                  <th>Cihaz</th>
                  <th>Durum</th>
                  <th>Süre</th>
                </tr>
              </thead>
              <tbody>
                {history.data.items.map((a) => {
                  return (
                    <tr key={a.id} className={a.id === detailId ? "selected" : undefined}>
                      <td>
                        <Link href={`/sites/${siteId}/alarms?alarm=${a.id}`}>{formatDateTime(a.opened_at, timeZone)}</Link>
                      </td>
                      <td>
                        <span className={`sev sev-${a.severity}`}>{SEVERITIES[a.severity]}</span>
                      </td>
                      <td>{a.rule_name}</td>
                      <td>{a.device_name}</td>
                      <td>{ALARM_STATES[a.state]}</td>
                      <td>{a.cleared_at ? formatDuration((Date.parse(a.cleared_at) - Date.parse(a.opened_at)) / 1000) : "Devam ediyor"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
        <div className="pager">
          <button type="button" disabled={filter.page <= 1} onClick={() => setFilter((f) => ({ ...f, page: f.page - 1 }))}>
            Önceki
          </button>
          <span className="muted">
            Sayfa {filter.page} / {pages}
            {history.data ? ` · ${history.data.total} alarm` : ""}
          </span>
          <button type="button" disabled={filter.page >= pages} onClick={() => setFilter((f) => ({ ...f, page: f.page + 1 }))}>
            Sonraki
          </button>
        </div>
      </section>

      {detailId ? <AlarmDetailPanel alarmId={detailId} timeZone={timeZone} onClose={() => router.push(`/sites/${siteId}/alarms`)} /> : null}
    </div>
  );
}

const EVENT_TEXT = { opened: "Açıldı", acknowledged: "Onaylandı", cleared: "Kapandı" } as const;

function AlarmDetailPanel({ alarmId, timeZone, onClose }: { alarmId: string; timeZone: string; onClose: () => void }) {
  const { me } = useSession();
  const detail = useAlarmDetail(alarmId);
  const ack = useAcknowledge();
  const a = detail.data;
  return (
    <section className="card detail" aria-labelledby="detail-title">
      <div className="card-head">
        <h2 id="detail-title">Alarm ayrıntısı</h2>
        <button type="button" onClick={onClose}>
          Kapat
        </button>
      </div>
      {detail.isPending ? <p className="muted">Yükleniyor…</p> : null}
      {detail.isError ? <p className="form-error">Bu alarm bulunamadı.</p> : null}
      {a ? (
        <>
          <p>
            <span className={`sev sev-${a.severity}`}>{SEVERITIES[a.severity]}</span> <strong>{a.rule_name}</strong>
          </p>
          <dl className="facts">
            <dt>Cihaz</dt>
            <dd>{a.device_name}</dd>
            <dt>Ölçüm</dt>
            <dd>{METRICS[a.metric].label}</dd>
            <dt>Durum</dt>
            <dd>{ALARM_STATES[a.state]}</dd>
            <dt>Tepe değer</dt>
            <dd>{a.peak_value !== null ? formatRuleValue(a.rule.kind, a.metric, a.peak_value) : "—"}</dd>
            <dt>Açılıştaki eşik</dt>
            <dd>
              {a.threshold !== null && a.clear_threshold !== null
                ? `${a.rule.operator} ${formatRuleValue(a.rule.kind, a.metric, a.threshold)} (kapanma eşiği ${formatRuleValue(a.rule.kind, a.metric, a.clear_threshold)})`
                : "Kayıtlı değil (alarm bu özellikten önce açıldı)"}
            </dd>
            {a.rule.kind === "reactive_ratio" ? (
              <>
                <dt>Kural</dt>
                <dd>Kayan {Math.round((a.rule.window_s ?? 0) / 60)} dk pencerede reaktif / etkin enerji oranı</dd>
              </>
            ) : null}
          </dl>
          <h3>Zaman çizelgesi</h3>
          <ol className="timeline">
            {a.timeline.map((e) => (
              <li key={e.event}>
                <strong>{EVENT_TEXT[e.event]}</strong> · {formatDateTime(e.at, timeZone)}
                {e.by ? <span className="muted"> · {e.by}</span> : null}
              </li>
            ))}
          </ol>
          {a.state === "active" && canWrite(me) ? (
            <button type="button" className="primary" disabled={ack.isPending} onClick={() => ack.mutate(a.id)}>
              {ack.isPending ? "Onaylanıyor…" : "Alarmı onayla"}
            </button>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
