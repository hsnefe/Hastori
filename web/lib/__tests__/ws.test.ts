import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveMessage } from "../types";
import { backoffDelay, CONNECT_MS, LiveConnection, SILENCE_MS, type ConnectionState, type SocketLike } from "../ws";

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  closed: number | null = null;
  send(data: string) {
    this.sent.push(data);
  }
  close(code = 1000) {
    this.closed = code;
  }
  // the "server"
  open() {
    this.onopen?.();
  }
  say(message: object) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
  drop(code = 1006) {
    this.onclose?.({ code });
  }
}

function setup(over: Partial<ConstructorParameters<typeof LiveConnection>[0]> = {}) {
  const sockets: FakeSocket[] = [];
  const states: ConnectionState[] = [];
  const messages: LiveMessage[] = [];
  let tickets = 0;
  const live = new LiveConnection({
    siteId: "site-1",
    getTicket: async () => `ticket-${++tickets}`,
    makeSocket: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
    onMessage: (m) => messages.push(m),
    onState: (s) => states.push(s),
    random: () => 1, // no jitter: the full delay
    ...over,
  });
  return { live, sockets, states, messages, tickets: () => tickets };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

describe("backoff", () => {
  it("doubles from 1 s to 30 s", () => {
    expect([0, 1, 2, 3, 4, 5, 6, 10].map((a) => backoffDelay(a, 1))).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000,
    ]);
  });
  it("is randomised between half and the full delay", () => {
    expect(backoffDelay(3, 0)).toBe(4000);
    expect(backoffDelay(3, 0.5)).toBe(6000);
    expect(backoffDelay(3, 1)).toBe(8000);
  });
});

describe("LiveConnection", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("gets a ticket, connects, subscribes and reports open on hello", async () => {
    const { live, sockets, states, messages } = setup();
    live.start();
    await flush();
    expect(states).toEqual(["connecting"]);
    sockets[0]!.open();
    expect(JSON.parse(sockets[0]!.sent[0]!)).toEqual({ type: "subscribe", site_id: "site-1" });
    sockets[0]!.say({ type: "hello", sites: ["site-1"], ts: 1 });
    sockets[0]!.say({ type: "resync" });
    sockets[0]!.say({ type: "hb" });
    expect(states).toEqual(["connecting", "open"]);
    expect(messages.map((m) => m.type)).toEqual(["hello", "resync"]); // heartbeats are not events
  });

  it("reconnects with a new ticket after the socket drops, waiting longer each time", async () => {
    const { live, sockets, states, tickets } = setup();
    live.start();
    await flush();
    sockets[0]!.open();
    sockets[0]!.drop();
    expect(states.at(-1)).toBe("backoff");
    await vi.advanceTimersByTimeAsync(999);
    expect(sockets.length).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets.length).toBe(2);
    expect(tickets()).toBe(2);
    sockets[1]!.drop(); // never said hello: the wait grows
    await vi.advanceTimersByTimeAsync(1999);
    expect(sockets.length).toBe(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets.length).toBe(3);
  });

  it("starts the backoff over once a connection really worked", async () => {
    const { live, sockets } = setup();
    live.start();
    await flush();
    sockets[0]!.drop();
    await vi.advanceTimersByTimeAsync(1000);
    sockets[1]!.drop();
    await vi.advanceTimersByTimeAsync(2000);
    sockets[2]!.open();
    sockets[2]!.say({ type: "hello", sites: [], ts: 1 });
    sockets[2]!.drop();
    await vi.advanceTimersByTimeAsync(1000); // back to the first step
    expect(sockets.length).toBe(4);
  });

  it("reconnects when a minute passes without any message", async () => {
    const { live, sockets, states } = setup();
    live.start();
    await flush();
    sockets[0]!.open();
    sockets[0]!.say({ type: "hello", sites: [], ts: 1 });
    await vi.advanceTimersByTimeAsync(SILENCE_MS - 1);
    sockets[0]!.say({ type: "hb" }); // a heartbeat resets the clock
    await vi.advanceTimersByTimeAsync(SILENCE_MS - 1);
    expect(sockets[0]!.closed).toBeNull();
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets[0]!.closed).not.toBeNull();
    expect(states.at(-1)).toBe("backoff");
    await vi.advanceTimersByTimeAsync(1000);
    expect(sockets.length).toBe(2);
  });

  it("does not come back for a site outside the scope (4403)", async () => {
    const { live, sockets, states } = setup();
    live.start();
    await flush();
    sockets[0]!.drop(4403);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(sockets.length).toBe(1);
    expect(states.at(-1)).toBe("refused");
  });

  it("does not come back when the server refuses the page's origin (1008), and says so", async () => {
    const { live, sockets, states } = setup();
    live.start();
    await flush();
    sockets[0]!.drop(1008);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(sockets.length).toBe(1);
    expect(states.at(-1)).toBe("refused");
  });

  it("gives up on a socket that never finishes connecting", async () => {
    const { live, sockets, states } = setup();
    live.start();
    await flush();
    await vi.advanceTimersByTimeAsync(CONNECT_MS - 1);
    expect(sockets[0]!.closed).toBeNull();
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets[0]!.closed).toBe(4000);
    expect(states.at(-1)).toBe("backoff");
    await vi.advanceTimersByTimeAsync(1000);
    expect(sockets.length).toBe(2); // and tries again
  });

  it("does not time out a socket that did open", async () => {
    const { live, sockets } = setup();
    live.start();
    await flush();
    sockets[0]!.open();
    sockets[0]!.say({ type: "hello", sites: [], ts: 1 });
    await vi.advanceTimersByTimeAsync(CONNECT_MS + 1000);
    expect(sockets[0]!.closed).toBeNull();
  });

  it("nudge() reconnects at once instead of waiting out a back-off", async () => {
    const { live, sockets } = setup();
    live.start();
    await flush();
    sockets[0]!.drop(1006);
    await vi.advanceTimersByTimeAsync(10); // in the 1 s back-off
    expect(sockets.length).toBe(1);
    live.nudge();
    await flush();
    expect(sockets.length).toBe(2);
  });

  it("nudge() does nothing on a connection that is fine or stopped", async () => {
    const { live, sockets } = setup();
    live.nudge(); // not started
    live.start();
    await flush();
    sockets[0]!.open();
    sockets[0]!.say({ type: "hello", sites: [], ts: 1 });
    live.nudge();
    await flush();
    expect(sockets.length).toBe(1);
  });

  it("retries when the ticket cannot be fetched", async () => {
    let fail = true;
    const { live, sockets } = setup({
      getTicket: async () => {
        if (fail) throw new Error("503");
        return "ok";
      },
    });
    live.start();
    await flush();
    expect(sockets.length).toBe(0);
    fail = false;
    await vi.advanceTimersByTimeAsync(1000);
    expect(sockets.length).toBe(1);
  });

  it("ignores everything a closed connection still says, and stop() ends the retries", async () => {
    const { live, sockets, messages } = setup();
    live.start();
    await flush();
    sockets[0]!.open();
    live.stop();
    expect(sockets[0]!.closed).toBe(1000);
    sockets[0]!.say({ type: "resync" });
    sockets[0]!.drop();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(messages).toEqual([]);
    expect(sockets.length).toBe(1);
  });

  it("a ticket that arrives after stop() is not used", async () => {
    let release: (t: string) => void = () => undefined;
    const { live, sockets } = setup({ getTicket: () => new Promise<string>((r) => (release = r)) });
    live.start();
    live.stop();
    release("late");
    await flush();
    expect(sockets.length).toBe(0);
  });
});
