"use client";

import dynamic from "next/dynamic";

import { formatDay, formatNumber, formatPercent } from "@/lib/format";
import { useConsumption } from "@/lib/hooks";

const EnergyBars = dynamic(() => import("./EnergyBars"), {
  ssr: false,
  loading: () => <div className="bars-box muted" />,
});

/** A day with less than this share of its minutes recorded is shown as partial, not as a total. */
export const COMPLETE_COVERAGE = 0.95;

export function EnergyCard({ siteId }: { siteId: string }) {
  const consumption = useConsumption(siteId);
  if (consumption.isPending) return <section className="card"><h2>Günlük enerji</h2><p className="muted">Yükleniyor…</p></section>;
  if (consumption.isError) {
    return (
      <section className="card" role="alert">
        <h2>Günlük enerji</h2>
        <p className="muted">Tüketim alınamadı.</p>
      </section>
    );
  }
  const days = consumption.data.days;
  const today = days[days.length - 1];
  if (!today || days.every((d) => d.coverage === 0)) {
    return (
      <section className="card">
        <h2>Günlük enerji</h2>
        <p className="muted">Bu tesiste ana pano ölçümü yok.</p>
      </section>
    );
  }
  const partial = today.coverage < COMPLETE_COVERAGE;
  return (
    <section className="card" aria-labelledby="energy-title">
      <h2 id="energy-title">Günlük enerji</h2>
      <p className="big-number">
        {formatNumber(today.kwh, 1)} <span className="unit">kWh</span>
      </p>
      <p className="muted">
        Bugün · veri kapsamı {formatPercent(today.coverage)}
        {partial ? " (gün henüz tamamlanmadı ya da eksik veri var)" : ""}
      </p>
      {today.reactive_ratio !== null ? (
        <p className="muted">Günlük reaktif oran {formatNumber(today.reactive_ratio, 3)}</p>
      ) : null}
      <EnergyBars
        data={days.map((d) => ({
          label: formatDay(d.date),
          kwh: d.kwh,
          coverage: d.coverage,
          complete: d.coverage >= COMPLETE_COVERAGE,
        }))}
      />
      <p className="legend muted">
        <span className="swatch" aria-hidden="true" /> tam gün <span className="swatch partial" aria-hidden="true" /> eksik / kısmi gün
      </p>
    </section>
  );
}
