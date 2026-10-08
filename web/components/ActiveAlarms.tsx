"use client";

import Link from "next/link";

import { useAcknowledge, useOpenAlarms } from "@/lib/alarms";
import { ALARM_STATES, formatDateTime, formatMetric, SEVERITIES } from "@/lib/format";
import { canWrite, useSession } from "@/lib/session";
import type { Alarm } from "@/lib/types";

/** Open alarms of the site, newest first. The acknowledge button exists only for roles the API
 * lets acknowledge (the API checks it again). */
export function ActiveAlarms({ siteId, compact = false }: { siteId: string; compact?: boolean }) {
  const { me } = useSession();
  const alarms = useOpenAlarms(siteId);
  const ack = useAcknowledge();
  const site = me?.sites.find((s) => s.id === siteId);
  const timeZone = site?.timezone ?? "Europe/Istanbul";

  return (
    <section className="card" aria-labelledby={`open-alarms-${compact ? "c" : "f"}`}>
      <div className="card-head">
        <h2 id={`open-alarms-${compact ? "c" : "f"}`}>
          Açık alarmlar{alarms.items.length ? <span className="count">{alarms.items.length}</span> : null}
        </h2>
        {compact ? (
          <Link href={`/sites/${siteId}/alarms`} className="link">
            Tümü
          </Link>
        ) : null}
      </div>
      {alarms.isPending ? <p className="muted">Yükleniyor…</p> : null}
      {alarms.isError ? (
        <p className="form-error" role="alert">
          Alarmlar alınamadı.{" "}
          <button type="button" className="link" onClick={() => void alarms.refetch()}>
            Tekrar dene
          </button>
        </p>
      ) : null}
      {!alarms.isPending && !alarms.isError && alarms.items.length === 0 ? (
        <p className="all-clear">Açık alarm yok.</p>
      ) : null}
      <ul className="alarm-list">
        {alarms.items.map((alarm) => (
          <AlarmRow
            key={alarm.id}
            alarm={alarm}
            timeZone={timeZone}
            siteId={siteId}
            canAck={canWrite(me)}
            busy={ack.isPending && ack.variables === alarm.id}
            onAck={() => ack.mutate(alarm.id)}
          />
        ))}
      </ul>
    </section>
  );
}

function AlarmRow({
  alarm,
  timeZone,
  siteId,
  canAck,
  busy,
  onAck,
}: {
  alarm: Alarm;
  timeZone: string;
  siteId: string;
  canAck: boolean;
  busy: boolean;
  onAck: () => void;
}) {
  return (
    <li className={`alarm-row alarm-${alarm.severity}`}>
      <div className="alarm-main">
        <span className={`sev sev-${alarm.severity}`}>{SEVERITIES[alarm.severity]}</span>
        <Link href={`/sites/${siteId}/alarms?alarm=${alarm.id}`} className="alarm-title">
          {alarm.rule_name}
        </Link>
        <span className="muted">
          {alarm.device_name} · {formatDateTime(alarm.opened_at, timeZone)}
          {alarm.peak_value !== null ? ` · tepe ${formatMetric(alarm.metric, alarm.peak_value)}` : ""}
        </span>
      </div>
      <div className="alarm-actions">
        {alarm.state === "acknowledged" ? (
          <span className="badge badge-idle">{ALARM_STATES.acknowledged}</span>
        ) : canAck ? (
          <button type="button" className="primary" disabled={busy} onClick={onAck}>
            {busy ? "Onaylanıyor…" : "Onayla"}
          </button>
        ) : (
          <span className="badge badge-warn">{ALARM_STATES.active}</span>
        )}
      </div>
    </li>
  );
}
