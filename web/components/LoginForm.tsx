"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { api, ApiError } from "@/lib/api";
import { formatDuration } from "@/lib/format";
import { useSession } from "@/lib/session";

export function loginErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "E-posta ya da parola hatalı.";
    if (error.status === 429) {
      const wait = error.retryAfterS ? ` ${formatDuration(error.retryAfterS)} sonra tekrar deneyin.` : "";
      return `Çok fazla hatalı deneme.${wait}`;
    }
    if (error.status === 503) return "Sunucu şu an hazır değil, birazdan tekrar deneyin.";
  }
  return "Giriş yapılamadı. Bağlantınızı kontrol edip tekrar deneyin.";
}

export function LoginForm() {
  const { status, me, login, demoLogin } = useSession();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void api.demoEnabled().then((on) => {
      if (!cancelled) setDemo(on);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (status === "authenticated") router.replace(me?.sites[0] ? `/sites/${me.sites[0].id}` : "/");
  }, [status, me, router]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
    } catch (e) {
      setError(loginErrorMessage(e));
      setBusy(false);
    }
  }

  async function enterAs(role: "viewer" | "site_admin") {
    setBusy(true);
    setError(null);
    try {
      await demoLogin(role);
    } catch (e) {
      setError(loginErrorMessage(e));
      setBusy(false);
    }
  }

  return (
    <main className="centered">
      <form className="card narrow" onSubmit={submit} aria-labelledby="login-title">
        <div className="brand brand-large">
          <span className="brand-mark" aria-hidden="true" />
          Hastori
        </div>
        <h1 id="login-title">Giriş yap</h1>
        <label>
          E-posta
          <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Parola
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="primary" disabled={busy || status === "loading"}>
          {busy ? "Giriş yapılıyor…" : "Giriş yap"}
        </button>
        {demo ? (
          <div className="demo-entry">
            <p className="muted">Demo: parola gerekmez. Tesis yöneticisi kuralları değiştirebilir ve alarmları onaylayabilir.</p>
            <div className="row-actions">
              <button type="button" disabled={busy || status === "loading"} onClick={() => void enterAs("viewer")}>
                İzleyici olarak gir
              </button>
              <button type="button" disabled={busy || status === "loading"} onClick={() => void enterAs("site_admin")}>
                Tesis yöneticisi olarak gir
              </button>
            </div>
          </div>
        ) : null}
      </form>
    </main>
  );
}
