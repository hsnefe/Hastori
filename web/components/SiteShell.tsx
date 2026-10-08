"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { LiveProvider, useLive } from "@/lib/live";
import { ROLES } from "@/lib/format";
import { useSession } from "@/lib/session";
import type { Site } from "@/lib/types";

import { ThemeToggle } from "./ThemeToggle";

const NAV = [{ href: "", label: "Panel" }];

/** Signed-in frame of every site page: header, site picker, connection state. */
export function SiteShell({ siteId, children }: { siteId: string; children: ReactNode }) {
  const { status, me } = useSession();
  const router = useRouter();

  useEffect(() => {
    if (status === "anonymous") router.replace("/login");
  }, [status, router]);

  if (status !== "authenticated" || !me) {
    return (
      <main className="centered">
        <p className="muted">Yükleniyor…</p>
      </main>
    );
  }
  const site = me.sites.find((s) => s.id === siteId);
  if (!site) {
    // A site outside the account looks like a site that does not exist, as in the API.
    return (
      <main className="centered">
        <div className="card narrow">
          <h1>Tesis bulunamadı</h1>
          <p className="muted">Bu tesise erişiminiz yok ya da böyle bir tesis yok.</p>
          {me.sites[0] ? (
            <Link className="button" href={`/sites/${me.sites[0].id}`}>
              {me.sites[0].name} tesisine git
            </Link>
          ) : null}
        </div>
      </main>
    );
  }
  return (
    <LiveProvider siteId={siteId}>
      <Header site={site} />
      <main className="page">{children}</main>
    </LiveProvider>
  );
}

function Header({ site }: { site: Site }) {
  const { me, logout } = useSession();
  const pathname = usePathname();
  const router = useRouter();
  const base = `/sites/${site.id}`;
  if (!me) return null;
  return (
    <header className="topbar">
      <div className="topbar-row">
        <Link href={base} className="brand">
          <span className="brand-mark" aria-hidden="true" />
          Hastori
        </Link>
        {me.sites.length > 1 ? (
          <label className="site-picker">
            <span className="visually-hidden">Tesis</span>
            <select
              value={site.id}
              onChange={(e) => router.push(`/sites/${e.target.value}${pathname.slice(base.length)}`)}
            >
              {me.sites.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <span className="site-name">{site.name}</span>
        )}
        <nav aria-label="Ana gezinme" className="nav">
          {NAV.map((item) => {
            const href = base + item.href;
            const active = item.href === "" ? pathname === base : pathname.startsWith(href);
            return (
              <Link key={item.href} href={href} className={active ? "nav-link active" : "nav-link"} aria-current={active ? "page" : undefined}>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="topbar-end">
          <ConnectionBadge />
          <span className="user" title={me.email}>
            {ROLES[me.role]}
          </span>
          <ThemeToggle />
          <button type="button" onClick={() => void logout().then(() => router.replace("/login"))}>
            Çıkış
          </button>
        </div>
      </div>
    </header>
  );
}

const STATE_TEXT = {
  open: "Canlı",
  connecting: "Bağlanıyor…",
  backoff: "Yeniden bağlanıyor…",
  closed: "Bağlantı yok",
} as const;

export function ConnectionBadge() {
  const { state } = useLive();
  // The first connect takes a moment; do not flash a warning for it.
  const [grace, setGrace] = useState(true);
  useEffect(() => {
    const t = setTimeout(() => setGrace(false), 2500);
    return () => clearTimeout(t);
  }, []);
  const kind = state === "open" ? "ok" : state === "connecting" && grace ? "idle" : "warn";
  return (
    <span className={`badge badge-${kind}`} role="status" aria-live="polite">
      <span className="dot" aria-hidden="true" />
      {STATE_TEXT[state]}
    </span>
  );
}
