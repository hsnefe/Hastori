import { NextResponse, type NextRequest } from "next/server";

// A Content Security Policy with a fresh nonce per request: only the scripts Next itself puts in
// the page run, an injected <script> does not. (Next 16 calls what used to be middleware a proxy.)
// Not applied in development: hot reload needs eval and the dev server talks to the API directly.
export function proxy(request: NextRequest) {
  if (process.env.NODE_ENV !== "production") return NextResponse.next();

  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const host = request.headers.get("host") ?? "";
  // Behind the HTTPS tunnel the gateway forwards the original scheme: then only wss may open.
  const https = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim() === "https";
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    // Styles: React and the chart library set style attributes, which a nonce cannot cover.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    // The page's own origin for REST, and its WebSocket (older browsers do not read 'self' as ws).
    https ? `connect-src 'self' wss://${host}` : `connect-src 'self' ws://${host} wss://${host}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      // Pages only: not the API (Caddy answers it), static files or prefetches.
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
