# ADR 0003: WebSocket, not Server-Sent Events, for live updates

Status: accepted (day 3)

## Context

The dashboard shows a live chart, device status and alarm events without a page refresh. One
browser shows one site at a time; the API must send each user only their sites' events, drop
sessions that should end, and survive a restart of any service behind it.

## Decision

One WebSocket at `/api/v1/ws`, opened with a single-use, 30-second ticket (`POST /ws-ticket`),
one site per connection. The server sends `hello`, then `measurement` and `alarm.*` events and a
25-second heartbeat; it tells the client to `resync` after a gap.

## Why WebSocket

- **A ticket makes the URL safe to use.** A browser cannot set an `Authorization` header
  on a WebSocket or an `EventSource`, so both need the credential in the URL or a cookie. A ticket
  worth nothing after one use (and 30 s) is safe in a query string, and Caddy drops it from the
  access log. With SSE the same ticket would work, but a long-lived cookie would not be an
  alternative here: the refresh cookie is `SameSite=Strict` and scoped to `/api/v1/auth`.
- **The close code carries the reason.** 4401 (no valid ticket), 4403 (a site outside the user's
  scope), 4408 (maximum age: reconnect with a new ticket) and 1013 (too slow) tell the client what
  to do next, and the browser's `WebSocket` exposes them. An `EventSource` only learns that the
  stream ended, and it reconnects on its own with the same URL, which would replay a used ticket.
- **Backpressure and limits are explicit.** Each socket has a bounded queue; a slow client is
  closed with 1013 and resynchronises instead of growing server memory (5 per user, 500 in total).

## Consequences

- A WebSocket needs the gateway to cooperate: Caddy proxies it as part of `/api/*`,
  `stream_close_delay` keeps sockets open through a reload, and the origin of the handshake is
  checked against `WS_ALLOWED_ORIGINS`.
- HTTP caches and plain `curl` cannot read the stream; tests use a real socket client.
- SSE would have been simpler for a broadcast-only feed without per-user scoping. The extra
  moving part buys the close codes and the ticket flow.
