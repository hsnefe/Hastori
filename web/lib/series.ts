// The chart's data: a fixed window of points, fed by REST once and by the socket afterwards.
// Pure functions, so the window logic is tested without a browser.

export interface Point {
  t: number; // epoch milliseconds
  v: number;
}

export const WINDOW_MS = 15 * 60 * 1000;

/** Drop what has slid out of the window (relative to the newest of `now` and the last point: a
 * device clock that runs ahead must not push its own latest points out). */
export function trim(points: Point[], nowMs: number, windowMs = WINDOW_MS): Point[] {
  const last = points.length ? points[points.length - 1]!.t : nowMs;
  const edge = Math.max(nowMs, last) - windowMs;
  let first = 0;
  while (first < points.length && points[first]!.t < edge) first++;
  return first === 0 ? points : points.slice(first);
}

/** Add live points to a REST snapshot: only those newer than the snapshot's last point, in order,
 * without a second point at the same instant. */
export function mergeLive(base: Point[], live: Point[]): Point[] {
  const lastBase = base.length ? base[base.length - 1]!.t : -Infinity;
  const out = base.slice();
  let last = lastBase;
  for (const p of live) {
    if (p.t > last) {
      out.push(p);
      last = p.t;
    }
  }
  return out;
}

/** Axis labels every 3 minutes, on round minutes (so "13:42, 13:45, ..." and not six labels that
 * all read 13:43). Half-hour zones shift only the label, not which instants are ticked, because
 * 3 minutes divides every UTC offset in use. */
export function ticks(left: number, right: number, stepMs = 180_000): number[] {
  const out: number[] = [];
  for (let t = Math.ceil(left / stepMs) * stepMs; t <= right; t += stepMs) out.push(t);
  return out;
}

/** The x axis: the full window, extended to the latest point when a device clock is ahead. */
export function domain(points: Point[], nowMs: number, windowMs = WINDOW_MS): [number, number] {
  const last = points.length ? points[points.length - 1]!.t : nowMs;
  const right = Math.max(nowMs, last);
  return [right - windowMs, right];
}
