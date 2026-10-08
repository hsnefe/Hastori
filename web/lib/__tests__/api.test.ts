import { describe, expect, it, vi } from "vitest";

import { ApiError, createApi } from "../api";

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

function harness(handler: Handler) {
  const calls: { url: string; init: RequestInit }[] = [];
  const sleeps: number[] = [];
  const api = createApi({
    fetch: (async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      return handler(url, init);
    }) as typeof fetch,
    sleep: async (ms) => {
      sleeps.push(ms);
    },
    base: "",
  });
  return { api, calls, sleeps };
}

describe("api", () => {
  it("sends the access token, which lives in memory only", async () => {
    const { api, calls } = harness((url) =>
      url.endsWith("/auth/login") ? json({ access_token: "tok-1", expires_in: 900 }) : json({ ok: true }),
    );
    await api.login("a@b.c", "pw");
    await api.get("/sites");
    expect((calls[1]!.init.headers as Record<string, string>).Authorization).toBe("Bearer tok-1");
    expect(JSON.stringify(Object.entries(localStorage))).not.toContain("tok-1");
    expect(JSON.stringify(Object.entries(sessionStorage))).not.toContain("tok-1");
    expect(document.cookie).not.toContain("tok-1");
  });

  it("refreshes once for parallel 401s and retries each request once", async () => {
    let refreshes = 0;
    const { api, calls } = harness((url, init) => {
      if (url.endsWith("/auth/refresh")) {
        refreshes++;
        return json({ access_token: "fresh", expires_in: 900 });
      }
      const auth = (init.headers as Record<string, string>).Authorization;
      return auth === "Bearer fresh" ? json({ ok: url }) : json({ error: { code: "unauthorized", message: "no" } }, 401);
    });
    const [a, b, c] = await Promise.all([api.get("/a"), api.get("/b"), api.get("/c")]);
    expect([a, b, c]).toEqual([{ ok: "/api/v1/a" }, { ok: "/api/v1/b" }, { ok: "/api/v1/c" }]);
    expect(refreshes).toBe(1);
    expect(calls.filter((c) => c.url.endsWith("/a")).length).toBe(2); // the failure and the retry
  });

  it("gives up after one retry and tells the page the session is gone", async () => {
    const { api, calls } = harness((url) =>
      url.endsWith("/auth/refresh")
        ? json({ error: { code: "unauthorized", message: "no session" } }, 401)
        : json({ error: { code: "unauthorized", message: "no" } }, 401),
    );
    const expired = vi.fn();
    api.onSessionExpired(expired);
    await expect(api.get("/sites")).rejects.toMatchObject({ status: 401 });
    expect(expired).toHaveBeenCalledTimes(1);
    expect(calls.length).toBe(2); // the request and one refresh: no second try without a token
  });

  it("does not refresh because a login failed", async () => {
    const { api, calls } = harness(() => json({ error: { code: "unauthorized", message: "Wrong" } }, 401));
    await expect(api.login("a@b.c", "bad")).rejects.toMatchObject({ status: 401, message: "Wrong" });
    expect(calls.length).toBe(1);
  });

  it("waits for Retry-After on a 503 and tries once more", async () => {
    let n = 0;
    const { api, sleeps } = harness(() =>
      n++ === 0 ? json({ error: { code: "unavailable", message: "db" } }, 503, { "Retry-After": "3" }) : json({ ok: 1 }),
    );
    expect(await api.get("/sites")).toEqual({ ok: 1 });
    expect(sleeps).toEqual([3000]);
  });

  it("does not wait more than 10 seconds, and not twice", async () => {
    const { api, sleeps } = harness(() =>
      json({ error: { code: "unavailable", message: "db" } }, 503, { "Retry-After": "120" }),
    );
    await expect(api.get("/sites")).rejects.toBeInstanceOf(ApiError);
    expect(sleeps).toEqual([10_000]);
  });

  it("decodes the standard error body, including the wait of a 429", async () => {
    const { api } = harness(() =>
      json({ error: { code: "too_many_requests", message: "Too many" } }, 429, { "Retry-After": "240" }),
    );
    const error = await api.login("a@b.c", "x").catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 429, code: "too_many_requests", retryAfterS: 240 });
  });

  it("survives an error page that is not JSON", async () => {
    const { api } = harness(() => new Response("<html>bad gateway</html>", { status: 502 }));
    await expect(api.get("/sites")).rejects.toMatchObject({ status: 502, code: "error" });
  });

  it("restore() says rejected when there is no session", async () => {
    const { api } = harness(() => json({ error: { code: "unauthorized", message: "No refresh token" } }, 401));
    expect(await api.restore()).toEqual({ kind: "rejected" });
    expect(api.token()).toBeNull();
  });

  describe("a refresh that fails for another reason than the session", () => {
    function signedIn(refresh: Handler) {
      const h = harness((url, init) => {
        if (url.endsWith("/auth/login")) return json({ access_token: "tok-1", expires_in: 900 });
        if (url.endsWith("/auth/refresh")) return refresh(url, init);
        return json({ error: { code: "unauthorized", message: "expired" } }, 401);
      });
      return h;
    }

    it("is not a sign-out when the server is down (5xx)", async () => {
      const { api } = signedIn(() => new Response("<html>bad gateway</html>", { status: 502 }));
      await api.login("a@b.c", "pw");
      const expired = vi.fn();
      api.onSessionExpired(expired);
      await expect(api.get("/sites")).rejects.toMatchObject({ status: 401 });
      expect(expired).not.toHaveBeenCalled();
      expect(api.token()).toBe("tok-1"); // the token is kept for the next try
    });

    it("is not a sign-out when the network is gone", async () => {
      const { api } = signedIn(() => {
        throw new TypeError("Failed to fetch");
      });
      await api.login("a@b.c", "pw");
      const expired = vi.fn();
      api.onSessionExpired(expired);
      await expect(api.get("/sites")).rejects.toMatchObject({ status: 401 });
      expect(expired).not.toHaveBeenCalled();
      expect(await api.restore()).toEqual({ kind: "unavailable" });
    });

    it("asks once more after a 503 with Retry-After (the server sent the new cookie with it)", async () => {
      let calls = 0;
      const { api, sleeps } = signedIn(() => {
        calls++;
        return calls === 1
          ? json({ error: { code: "unavailable", message: "busy" } }, 503, { "Retry-After": "2" })
          : json({ access_token: "fresh", expires_in: 900 });
      });
      expect(await api.restore()).toEqual({ kind: "ok", token: "fresh" });
      expect(calls).toBe(2);
      expect(sleeps).toEqual([2000]);
    });

    it("is a sign-out only when the server rejects the cookie (401)", async () => {
      const { api } = signedIn(() => json({ error: { code: "unauthorized", message: "reused" } }, 401));
      await api.login("a@b.c", "pw");
      const expired = vi.fn();
      api.onSessionExpired(expired);
      await expect(api.get("/sites")).rejects.toMatchObject({ status: 401 });
      expect(expired).toHaveBeenCalledTimes(1);
    });
  });
});
