"use client";

import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis } from "recharts";

import { formatNumber, formatPercent } from "@/lib/format";

interface Bar7 {
  label: string;
  kwh: number;
  coverage: number;
  complete: boolean;
}

export default function EnergyBars({ data }: { data: Bar7[] }) {
  return (
    <div className="bars-box" role="img" aria-label="Son 7 günün kWh değerleri">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
          <XAxis dataKey="label" stroke="var(--muted)" fontSize={11} tickLine={false} interval={0} />
          <Tooltip
            cursor={{ fill: "var(--grid)" }}
            isAnimationActive={false}
            formatter={(v, _name, item) => {
              const row = item.payload as Bar7;
              return [`${formatNumber(Number(v), 1)} kWh · kapsam ${formatPercent(row.coverage)}`, row.complete ? "Tam gün" : "Eksik / kısmi gün"];
            }}
            contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}
          />
          <Bar dataKey="kwh" isAnimationActive={false} radius={[3, 3, 0, 0]}>
            {data.map((d) => (
              <Cell key={d.label} fill="var(--series)" fillOpacity={d.complete ? 1 : 0.35} stroke="var(--series)" strokeDasharray={d.complete ? undefined : "3 2"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
