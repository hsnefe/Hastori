// A thin fetch wrapper around the REST API.
//
// The access token lives in this module's memory and nowhere else (not in localStorage, not in a
// cookie JavaScript can read): a page reload gets a new one from the httpOnly refresh cookie.
// A 401 triggers exactly one refresh, shared by every request that failed at the same time, and
// one retry. A 503 waits for Retry-After and tries once more.

import type { TokenOut } from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryAfterS: number | null = null,
    readonly details: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** What a refresh came to. Only `rejected` means the session is over: a down server, a proxy
 * error or a lost connection says nothing about whether the cookie is still good. */
export type RefreshResult = { kind: "ok"; token: string } | { kind: "rejected" } | { kind: "unavailable" };

export interface ApiDeps {
  fetch: typeof fetch;
  sleep: (ms: number) => Promise<void>;
  base: string;
}

const MAX_RETRY_AFTER_S = 10;

export function createApi(deps: ApiDeps) {
  let accessToken: string | null = null;
  let refreshing: Promise<RefreshResult> | null = null;
  const expiredListeners = new Set<() => void>();

  async function errorFrom(res: Response): Promise<ApiError> {
    let code = "error";
    let message = `HTTP ${res.status}`;
    let details: unknown = null;
    try {
      const body = (await res.json()) as { error?: { code?: string; message?: string; details?: unknown } };
      code = body.error?.code ?? code;
      message = body.error?.message ?? message;
      details = body.error?.details ?? null;
    } catch {
      // not JSON (a proxy error page): keep the status text
    }
    const header = Number(res.headers.get("Retry-After"));
    return new ApiError(res.status, code, message, Number.isFinite(header) && header > 0 ? header : null, details);
  }

  /** One refresh at a time: parallel 401s (and parallel tabs' worth of effects) share it. */
  function refresh(): Promise<RefreshResult> {
    refreshing ??= (async (): Promise<RefreshResult> => {
      try {
        for (let attempt = 0; ; attempt++) {
          const res = await deps.fetch(`${deps.base}/api/v1/auth/refresh`, { method: "POST" });
          if (res.ok) {
            const token = ((await res.json()) as TokenOut).access_token;
            accessToken = token;
            return { kind: "ok", token };
          }
          if (res.status === 401 || res.status === 403) {
            accessToken = null;
            return { kind: "rejected" };
          }
          // A backing service is down (503 + Retry-After): the server already rotated the
          // cookie and sent the new one with the error, so asking again once is safe.
          const wait = Number(res.headers.get("Retry-After"));
          if (res.status === 503 && attempt === 0 && Number.isFinite(wait) && wait > 0) {
            await deps.sleep(Math.min(wait, MAX_RETRY_AFTER_S) * 1000);
            continue;
          }
          return { kind: "unavailable" };
        }
      } catch {
        return { kind: "unavailable" }; // the network, not the session: keep the token we have
      } finally {
        refreshing = null;
      }
    })();
    return refreshing;
  }

  async function request<T>(
    method: string,
    path: string,
    body?: unknown,
    opts: { auth?: boolean } = {},
  ): Promise<T> {
    const auth = opts.auth ?? true;
    let refreshed = false;
    let waited = false;
    for (;;) {
      const headers: Record<string, string> = {};
      if (body !== undefined) headers["Content-Type"] = "application/json";
      if (auth && accessToken) headers.Authorization = `Bearer ${accessToken}`;
      const res = await deps.fetch(`${deps.base}/api/v1${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (res.ok) return (res.status === 204 ? undefined : await res.json()) as T;
      if (res.status === 401 && auth && !refreshed) {
        refreshed = true;
        const result = await refresh();
        if (result.kind === "ok") continue;
        if (result.kind === "rejected") expiredListeners.forEach((fn) => fn());
      }
      const error = await errorFrom(res);
      if (res.status === 503 && !waited && error.retryAfterS !== null) {
        waited = true;
        await deps.sleep(Math.min(error.retryAfterS, MAX_RETRY_AFTER_S) * 1000);
        continue;
      }
      throw error;
    }
  }

  return {
    get: <T>(path: string) => request<T>("GET", path),
    post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
    put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
    /** The access token for the session in this tab, or null. */
    token: () => accessToken,
    /** Sign in: stores the access token (the refresh cookie is set by the server). */
    async login(email: string, password: string): Promise<void> {
      const out = await request<TokenOut>("POST", "/auth/login", { email, password }, { auth: false });
      accessToken = out.access_token;
    },
    /** Whether the server offers the passwordless demo buttons (false when it cannot be asked). */
    async demoEnabled(): Promise<boolean> {
      try {
        return (await request<{ enabled: boolean }>("GET", "/auth/demo", undefined, { auth: false })).enabled;
      } catch {
        return false;
      }
    },
    /** Sign in as the demo viewer or the demo site admin (only when the server has it on). */
    async demoLogin(role: "viewer" | "site_admin"): Promise<void> {
      const out = await request<TokenOut>("POST", "/auth/demo", { role }, { auth: false });
      accessToken = out.access_token;
    },
    /** Called on page load: a token from the refresh cookie, no session, or "unavailable". */
    restore: refresh,
    async logout(): Promise<void> {
      try {
        await request<void>("POST", "/auth/logout", undefined, { auth: false });
      } finally {
        accessToken = null;
      }
    },
    /** Called when a request failed with 401 and the session could not be renewed. */
    onSessionExpired(fn: () => void): () => void {
      expiredListeners.add(fn);
      return () => expiredListeners.delete(fn);
    },
  };
}

export type Api = ReturnType<typeof createApi>;

export const api: Api = createApi({
  fetch: (...args) => fetch(...args),
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  base: "",
});
