import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import { useEffect, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import { applyMeasurement, LiveProvider, useLive, wsUrl, type LiveDevice } from "../live";
import { ToastProvider } from "../toast";
import type { MeasurementEvent } from "../types";

const device = (id: string): LiveDevice => ({
  id,
  site_id: "s1",
  key: id,
  name: id,
  type: "compressor",
  is_active: true,
  online: false,
  last_seen: null,
  latest: { temperature_c: { value: 60, time: "2026-10-08T10:00:00Z" } },
});

const event = (over: Partial<MeasurementEvent> = {}): MeasurementEvent => ({
  type: "measurement",
  device_id: "d1",
  ts: 1_791_000_000,
  metrics: { temperature_c: 71.5, active_power_kw: 12 },
  ...over,
});

describe("applyMeasurement", () => {
  it("patches only the device of the event, and marks it online as of its arrival", () => {
    const out = applyMeasurement([device("d1"), device("d2")], event(), 42_000)!;
    expect(out[0]!.latest.temperature_c!.value).toBe(71.5);
    expect(out[0]!.latest.active_power_kw!.value).toBe(12);
    expect(out[0]!.online).toBe(true);
    expect(out[0]!.arrivedAt).toBe(42_000);
    expect(out[1]).toEqual(device("d2"));
  });

  it("keeps the other metrics of the device", () => {
    const out = applyMeasurement([device("d1")], event({ metrics: { active_power_kw: 3 } }), 1)!;
    expect(out[0]!.latest.temperature_c!.value).toBe(60);
  });

  it("does nothing before the device list has been fetched", () => {
    expect(applyMeasurement(undefined, event(), 1)).toBeUndefined();
  });
});

describe("wsUrl", () => {
  it("uses the configured URL (development: straight to the API) and encodes the ticket", () => {
    expect(wsUrl("a b", "ws://127.0.0.1:8000/api/v1/ws")).toBe("ws://127.0.0.1:8000/api/v1/ws?ticket=a%20b");
  });
  it("falls back to the page's own origin (packaged: behind Caddy)", () => {
    expect(wsUrl("t", "")).toBe(`ws://${window.location.host}/api/v1/ws?ticket=t`);
  });
});

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {}
  say(message: object) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
}

describe("LiveProvider", () => {
  let client: QueryClient;
  const seen: MeasurementEvent[] = [];

  function Probe() {
    const live = useLive();
    useEffect(() => live.subscribe((e) => seen.push(e)), [live]);
    return <span>{live.state}</span>;
  }

  function wrap({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <ToastProvider>{children}</ToastProvider>
      </QueryClientProvider>
    );
  }

  async function mount() {
    const view = render(
      <LiveProvider siteId="s1">
        <Probe />
      </LiveProvider>,
      { wrapper: wrap },
    );
    await act(async () => {
      await Promise.resolve();
    });
    const socket = FakeWebSocket.instances.at(-1)!;
    act(() => {
      socket.onopen?.();
      socket.say({ type: "hello", sites: ["s1"], ts: 1 });
    });
    return { view, socket };
  }

  beforeEach(() => {
    FakeWebSocket.instances = [];
    seen.length = 0;
    client = new QueryClient();
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.spyOn(api, "post").mockResolvedValue({ ticket: "tk-1" });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("connects with a ticket from the API and subscribes to its site", async () => {
    const { socket, view } = await mount();
    expect(socket.url).toContain("ticket=tk-1");
    expect(JSON.parse(socket.sent[0]!)).toEqual({ type: "subscribe", site_id: "s1" });
    expect(view.getByText("open")).toBeInTheDocument();
  });

  it("an alarm event makes the alarm queries stale; a measurement does not", async () => {
    const { socket } = await mount();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    act(() => socket.say({ type: "measurement", device_id: "d1", ts: 1, metrics: { temperature_c: 1 } }));
    expect(invalidate).not.toHaveBeenCalled();
    act(() =>
      socket.say({
        type: "alarm.cleared",
        alarm_id: "a1",
        rule_id: "r1",
        rule_name: "r",
        device_id: "d1",
        site_id: "s1",
        severity: "critical",
        state: "cleared",
        value: 80,
        ts: 1,
      }),
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["alarms"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["alarm", "a1"] });
  });

  it("resync makes everything stale (REST is the truth)", async () => {
    const { socket } = await mount();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    act(() => socket.say({ type: "resync" }));
    expect(invalidate).toHaveBeenCalledWith();
  });

  it("a measurement patches the device cards in the cache and reaches chart subscribers", async () => {
    client.setQueryData(["devices", "s1"], [device("d1")]);
    const { socket } = await mount();
    act(() => socket.say(event()));
    const cached = client.getQueryData<LiveDevice[]>(["devices", "s1"])!;
    expect(cached[0]!.latest.temperature_c!.value).toBe(71.5);
    expect(seen.length).toBe(1);
  });

  it("only an alarm that just opened is announced, not one a reconnect replays", async () => {
    const { socket, view } = await mount();
    const opened = (ts: number, name: string) => ({
      type: "alarm.opened",
      alarm_id: name,
      rule_id: "r",
      rule_name: name,
      device_id: "d1",
      site_id: "s1",
      severity: "critical",
      state: "active",
      value: 85,
      ts,
    });
    act(() => socket.say(opened(Date.now() / 1000 - 3600, "old-one")));
    expect(view.queryByText(/old-one/)).toBeNull();
    act(() => socket.say(opened(Date.now() / 1000 - 5, "fresh-one")));
    expect(view.getByText(/fresh-one/)).toBeInTheDocument();
  });
});
