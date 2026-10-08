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

export interface ApiDeps {
  fetch: typeof fetch;
  sleep: (ms: number) => Promise<void>;
  base: string;
}

const MAX_RETRY_AFTER_S = 10;

export function createApi(deps: ApiDeps) {
  let accessToken: string | null = null;
  let refreshing: Promise<string | null> | null = null;
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
  function refresh(): Promise<string | null> {
    refreshing ??= (async () => {
      try {
        const res = await deps.fetch(`${deps.base}/api/v1/auth/refresh`, { method: "POST" });
        if (!res.ok) {
          accessToken = null;
          return null;
        }
        accessToken = ((await res.json()) as TokenOut).access_token;
        return accessToken;
      } catch {
        return null; // the network, not the session: keep the token we have
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
        if (await refresh()) continue;
        expiredListeners.forEach((fn) => fn());
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
    /** Called once on page load: a token from the refresh cookie, or null when there is no session. */
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
