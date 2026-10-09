// @vitest-environment node
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { proxy } from "../../proxy";

function csp(headers: Record<string, string>): string {
  vi.stubEnv("NODE_ENV", "production");
  const response = proxy(new NextRequest("http://hastori.test/", { headers }));
  return response.headers.get("Content-Security-Policy") ?? "";
}

describe("Content Security Policy", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("allows ws and wss to the page's own host on plain http", () => {
    const policy = csp({ host: "127.0.0.1:8080" });
    expect(policy).toContain("connect-src 'self' ws://127.0.0.1:8080 wss://127.0.0.1:8080");
  });

  it("allows only wss once the gateway says the request came over https", () => {
    const policy = csp({ host: "hastori.example", "x-forwarded-proto": "https" });
    expect(policy).toContain("connect-src 'self' wss://hastori.example;");
    expect(policy).not.toContain("ws://");
  });

  it("uses a fresh nonce per request", () => {
    const nonce = (p: string) => /'nonce-([^']+)'/.exec(p)?.[1];
    expect(nonce(csp({ host: "a" }))).not.toBe(nonce(csp({ host: "a" })));
  });
});
