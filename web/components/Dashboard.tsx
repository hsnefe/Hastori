"use client";

import { useState } from "react";

import { ApiError } from "@/lib/api";
import { DEVICE_TYPES, formatMetric, METRIC_ORDER, METRICS, formatTime } from "@/lib/format";
import { isOnline, useDevices, useNow } from "@/lib/hooks";
import type { LiveDevice } from "@/lib/live";
import { useSession } from "@/lib/session";
import type { Metric } from "@/lib/types";

import { ActiveAlarms } from "./ActiveAlarms";
import { EnergyCard } from "./EnergyCard";
import { LiveChart } from "./LiveChart";

/** The default chart: the main panel's active power (the energy analyzer). */
export function defaultDevice(devices: LiveDevice[]): LiveDevice | undefined {
  return devices.find((d) => d.type === "energy_analyzer") ?? devices[0];
}

export function Dashboard({ siteId }: { siteId: string }) {
  const { me } = useSession();
  const devices = useDevices(siteId);
  const now = useNow(1000);
  const [picked, setPicked] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>("active_power_kw");
  const site = me?.sites.find((s) => s.id === siteId);

  if (devices.isPending) return <p className="muted">Cihazlar yükleniyor…</p>;
  if (devices.isError) {
    const unavailable = devices.error instanceof ApiError && devices.error.status >= 500;
    return (
      <div className="card" role="alert">
        <h2>Cihazlar alınamadı</h2>
        <p className="muted">{unavailable ? "Sunucu şu an hazır değil." : "Bir sorun oluştu."}</p>
        <button type="button" onClick={() => void devices.refetch()}>
          Tekrar dene
        </button>
      </div>
    );
  }
  const list = devices.data;
  const selected = list.find((d) => d.id === picked) ?? defaultDevice(list);

  return (
    <div className="dashboard">
      <section aria-labelledby="devices-title" className="span-all">
        <h2 id="devices-title">Cihazlar</h2>
        <div className="device-grid">
          {list.map((device) => (
            <DeviceCard
              key={device.id}
              device={device}
              online={isOnline(device, now)}
              selected={device.id === selected?.id}
              onSelect={() => setPicked(device.id)}
              timeZone={site?.timezone ?? "Europe/Istanbul"}
            />
          ))}
        </div>
      </section>

      <section aria-labelledby="chart-title" className="card span-chart">
        <div className="card-head">
          <h2 id="chart-title">Canlı grafik</h2>
          <div className="controls">
            <label>
              <span className="visually-hidden">Cihaz</span>
              <select value={selected?.id ?? ""} onChange={(e) => setPicked(e.target.value)}>
                {list.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span className="visually-hidden">Ölçüm</span>
              <select value={metric} onChange={(e) => setMetric(e.target.value as Metric)}>
                {METRIC_ORDER.map((m) => (
                  <option key={m} value={m}>
                    {METRICS[m].label} ({METRICS[m].unit})
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
        <LiveChart deviceId={selected?.id} metric={metric} timeZone={site?.timezone ?? "Europe/Istanbul"} />
      </section>

      <div className="side">
        <EnergyCard siteId={siteId} />
        <ActiveAlarms siteId={siteId} compact />
      </div>
    </div>
  );
}

function DeviceCard({
  device,
  online,
  selected,
  onSelect,
  timeZone,
}: {
  device: LiveDevice;
  online: boolean;
  selected: boolean;
  onSelect: () => void;
  timeZone: string;
}) {
  return (
    <button
      type="button"
      className={`device-card${selected ? " selected" : ""}${online ? "" : " offline"}`}
      onClick={onSelect}
      aria-pressed={selected}
    >
      <span className="device-head">
        <span>
          <span className="device-name">{device.name}</span>
          <span className="device-type">{DEVICE_TYPES[device.type] ?? device.type}</span>
        </span>
        <span className={`badge badge-${online ? "ok" : "off"}`}>
          <span className="dot" aria-hidden="true" />
          {online ? "Çevrimiçi" : "Çevrimdışı"}
        </span>
      </span>
      <span className="metric-list">
        {METRIC_ORDER.map((m) => {
          const latest = device.latest[m];
          return (
            <span key={m} className="metric">
              <span className="metric-label">{METRICS[m].label}</span>
              <span className="metric-value">{latest ? formatMetric(m, latest.value) : "—"}</span>
            </span>
          );
        })}
      </span>
      {device.last_seen ? (
        <span className="device-seen">Son ölçüm {formatTime(device.last_seen, timeZone)}</span>
      ) : (
        <span className="device-seen">Henüz ölçüm yok</span>
      )}
    </button>
  );
}
