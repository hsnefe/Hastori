// The live connection: ticket -> socket -> subscribe -> messages, and getting back after a break.
//
// Messages are hints. `resync` (sent after every subscription and whenever the server's Redis
// link came back) means "ask REST for the current state"; nothing here tries to repair a gap.

import type { LiveMessage } from "./types";

export type ConnectionState = "connecting" | "open" | "backoff" | "closed";

/** The part of WebSocket this module uses (so tests can play the server). */
export interface SocketLike {
  onopen: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: (() => void) | null;
  send(data: string): void;
  close(code?: number): void;
}

export interface LiveOptions {
  siteId: string;
  getTicket: () => Promise<string>;
  makeSocket: (ticket: string) => SocketLike;
  onMessage: (message: LiveMessage) => void;
  onState: (state: ConnectionState) => void;
  random?: () => number;
  /** No message at all (the server sends a heartbeat every 25 s) for this long = a dead link. */
  silenceMs?: number;
}

export const SILENCE_MS = 60_000;
const BACKOFF_MIN_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
// The site is not ours to see: retrying cannot help.
const CLOSE_FORBIDDEN = 4403;

/** 1 s, 2 s, 4 s ... up to 30 s, each scaled by 50-100 %: a restarted server is not hit by every
 * screen at the same instant. */
export function backoffDelay(attempt: number, random: number): number {
  const base = Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** attempt);
  return Math.round(base * (0.5 + random / 2));
}

export class LiveConnection {
  private socket: SocketLike | null = null;
  private attempt = 0;
  private generation = 0; // a socket or ticket request from before stop()/reconnect is ignored
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private silenceTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;

  constructor(private readonly options: LiveOptions) {}

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.generation++;
    this.clearTimers();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      socket.close(1000);
    }
    this.options.onState("closed");
  }

  private clearTimers(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    this.retryTimer = this.silenceTimer = null;
  }

  private async connect(): Promise<void> {
    const generation = ++this.generation;
    this.options.onState("connecting");
    let ticket: string;
    try {
      ticket = await this.options.getTicket();
    } catch {
      if (generation === this.generation) this.scheduleRetry();
      return;
    }
    if (generation !== this.generation) return;
    const socket = this.options.makeSocket(ticket);
    this.socket = socket;
    socket.onopen = () => {
      if (generation !== this.generation) return;
      socket.send(JSON.stringify({ type: "subscribe", site_id: this.options.siteId }));
      this.armSilence(generation);
    };
    socket.onmessage = (event) => {
      if (generation !== this.generation) return;
      this.armSilence(generation);
      let message: LiveMessage;
      try {
        message = JSON.parse(String(event.data)) as LiveMessage;
      } catch {
        return;
      }
      if (message.type === "hello") {
        this.attempt = 0;
        this.options.onState("open");
      }
      if (message.type !== "hb") this.options.onMessage(message);
    };
    socket.onerror = () => undefined; // onclose follows
    socket.onclose = (event) => {
      if (generation !== this.generation) return;
      this.socket = null;
      if (this.silenceTimer) clearTimeout(this.silenceTimer);
      if (event.code === CLOSE_FORBIDDEN) {
        this.stopped = true;
        this.options.onState("closed");
        return;
      }
      this.scheduleRetry();
    };
  }

  private armSilence(generation: number): void {
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    this.silenceTimer = setTimeout(() => {
      if (generation !== this.generation) return;
      const socket = this.socket;
      this.socket = null;
      if (socket) {
        socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
        socket.close(4000);
      }
      this.scheduleRetry();
    }, this.options.silenceMs ?? SILENCE_MS);
  }

  private scheduleRetry(): void {
    if (this.stopped) return;
    this.generation++; // whatever was in flight is stale now
    this.options.onState("backoff");
    const delay = backoffDelay(this.attempt++, (this.options.random ?? Math.random)());
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (!this.stopped) void this.connect();
    }, delay);
  }
}
