import { describe, expect, it } from "vitest";

import { domain, mergeLive, ticks, trim, WINDOW_MS, type Point } from "../series";

const pts = (...ts: number[]): Point[] => ts.map((t) => ({ t, v: t }));

describe("series window", () => {
  it("drops points that slid out of the 15 minute window", () => {
    const now = 10_000_000;
    const kept = trim(pts(now - WINDOW_MS - 1, now - WINDOW_MS, now - 5, now), now);
    expect(kept.map((p) => p.t)).toEqual([now - WINDOW_MS, now - 5, now]);
  });

  it("returns the same array when nothing is old (no needless copy)", () => {
    const now = 10_000_000;
    const list = pts(now - 10, now);
    expect(trim(list, now)).toBe(list);
  });

  it("keeps the points of a device clock that runs ahead", () => {
    const now = 10_000_000;
    const ahead = now + 30_000;
    const kept = trim(pts(now - WINDOW_MS + 40_000, ahead), now);
    expect(kept.length).toBe(2);
    expect(domain(kept, now)[1]).toBe(ahead); // the axis reaches the latest point
  });

  it("has a bounded size however long it runs", () => {
    let list: Point[] = [];
    const start = 1_000_000;
    for (let i = 0; i < 5000; i++) list = trim([...list, { t: start + i * 2000, v: i }], start + i * 2000);
    expect(list.length).toBeLessThanOrEqual(WINDOW_MS / 2000 + 1);
  });

  it("merges live points after a snapshot without duplicates or going back", () => {
    const merged = mergeLive(pts(1000, 2000, 3000), pts(2000, 3000, 4000, 3500, 5000));
    expect(merged.map((p) => p.t)).toEqual([1000, 2000, 3000, 4000, 5000]);
  });

  it("starts from live points when there is no snapshot", () => {
    expect(mergeLive([], pts(1, 2)).map((p) => p.t)).toEqual([1, 2]);
  });
});

describe("axis ticks", () => {
  it("ticks every 3 minutes on round minutes, inside the window", () => {
    const left = Date.UTC(2026, 9, 8, 10, 1, 20);
    const right = left + WINDOW_MS;
    const t = ticks(left, right);
    expect(t.length).toBe(5);
    expect(t.every((x) => x % 180_000 === 0 && x >= left && x <= right)).toBe(true);
    expect(new Date(t[0]!).toISOString()).toBe("2026-10-08T10:03:00.000Z");
  });
});
