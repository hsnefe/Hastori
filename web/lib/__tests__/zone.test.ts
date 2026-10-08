import { describe, expect, it } from "vitest";

import { startOfDay, startOfNextDay, zoneOffsetMs } from "../zone";

const HOUR = 3_600_000;

describe("zone days", () => {
  it("Istanbul is UTC+3 all year", () => {
    expect(zoneOffsetMs(Date.UTC(2026, 0, 15), "Europe/Istanbul")).toBe(3 * HOUR);
    expect(zoneOffsetMs(Date.UTC(2026, 6, 15), "Europe/Istanbul")).toBe(3 * HOUR);
  });

  it("a day begins at local midnight, which is the evening before in UTC", () => {
    expect(new Date(startOfDay("2026-10-08", "Europe/Istanbul")).toISOString()).toBe("2026-10-07T21:00:00.000Z");
  });

  it("follows daylight saving time", () => {
    // Berlin: UTC+2 in summer, UTC+1 in winter
    expect(new Date(startOfDay("2026-07-01", "Europe/Berlin")).toISOString()).toBe("2026-06-30T22:00:00.000Z");
    expect(new Date(startOfDay("2026-12-01", "Europe/Berlin")).toISOString()).toBe("2026-11-30T23:00:00.000Z");
  });

  it("a day is not always 24 hours", () => {
    const day = (d: string) => startOfNextDay(d, "Europe/Berlin") - startOfDay(d, "Europe/Berlin");
    expect(day("2026-10-25")).toBe(25 * HOUR); // clocks go back
    expect(day("2026-03-29")).toBe(23 * HOUR); // clocks go forward
    expect(day("2026-10-08")).toBe(24 * HOUR);
  });

  it("the next day of a month's last day is the first of the next month", () => {
    expect(new Date(startOfNextDay("2026-10-31", "UTC")).toISOString()).toBe("2026-11-01T00:00:00.000Z");
  });
});
