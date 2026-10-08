"use client";

import dynamic from "next/dynamic";

import { METRICS } from "@/lib/format";
import { useLiveSeries, useNow } from "@/lib/hooks";
import type { Metric } from "@/lib/types";

// Recharts measures the DOM; it never runs on the server.
const PowerChart = dynamic(() => import("./PowerChart"), {
  ssr: false,
  loading: () => <div className="chart-box muted">Grafik yükleniyor…</div>,
});

export function LiveChart({ deviceId, metric, timeZone }: { deviceId: string | undefined; metric: Metric; timeZone: string }) {
  const { points, loading } = useLiveSeries(deviceId, metric);
  const now = useNow(1000);
  if (!deviceId) return <div className="chart-box muted">Bu tesiste cihaz yok.</div>;
  if (loading) return <div className="chart-box muted">Ölçümler yükleniyor…</div>;
  return (
    <div className="chart-box" role="img" aria-label={`${METRICS[metric].label}, son 15 dakika`}>
      <PowerChart points={points} now={now} unit={METRICS[metric].unit} label={METRICS[metric].label} timeZone={timeZone} />
      {points.length === 0 ? <p className="chart-empty muted">Son 15 dakikada ölçüm yok.</p> : null}
    </div>
  );
}
