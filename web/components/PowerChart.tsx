"use client";

import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { formatNumber, formatTime } from "@/lib/format";
import { domain, ticks, type Point } from "@/lib/series";

// No animation, no dots: at 450 points a redraw per second must stay cheap.
export default function PowerChart({
  points,
  now,
  unit,
  label,
  timeZone,
}: {
  points: Point[];
  now: number;
  unit: string;
  label: string;
  timeZone: string;
}) {
  const [left, right] = domain(points, now);
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={points} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="var(--grid)" vertical={false} />
        <XAxis
          dataKey="t"
          type="number"
          scale="time"
          domain={[left, right]}
          allowDataOverflow
          ticks={ticks(left, right)}
          tickFormatter={(t: number) => formatTime(t, timeZone, false)}
          stroke="var(--muted)"
          fontSize={12}
        />
        <YAxis
          width={52}
          domain={["auto", "auto"]}
          tickFormatter={(v: number) => formatNumber(v, 0)}
          stroke="var(--muted)"
          fontSize={12}
          unit={` ${unit}`}
          hide={false}
        />
        <Tooltip
          isAnimationActive={false}
          labelFormatter={(t) => formatTime(Number(t), timeZone)}
          formatter={(v) => [`${formatNumber(Number(v), 1)} ${unit}`, label]}
          contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}
        />
        <Line type="monotone" dataKey="v" stroke="var(--series)" strokeWidth={2} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
