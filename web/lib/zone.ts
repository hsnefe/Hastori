// Calendar days in a site's time zone (the filter "from 8 Oct" means that site's 8 Oct).

/** Milliseconds the zone is ahead of UTC at the instant `ms`. */
export function zoneOffsetMs(ms: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(ms));
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  return asUtc - Math.floor(ms / 1000) * 1000;
}

/** The instant (epoch ms) a calendar day begins in `timeZone`; `isoDate` is "yyyy-mm-dd". */
export function startOfDay(isoDate: string, timeZone: string): number {
  const [y, m, d] = isoDate.split("-").map(Number) as [number, number, number];
  const guess = Date.UTC(y, m - 1, d);
  // Offsets differ across a daylight-saving change: use the one in force at the result.
  const first = guess - zoneOffsetMs(guess, timeZone);
  return guess - zoneOffsetMs(first, timeZone);
}

/** The instant the next calendar day begins (an exclusive upper bound for "up to this day"). */
export function startOfNextDay(isoDate: string, timeZone: string): number {
  const [y, m, d] = isoDate.split("-").map(Number) as [number, number, number];
  const next = new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10);
  return startOfDay(next, timeZone);
}
