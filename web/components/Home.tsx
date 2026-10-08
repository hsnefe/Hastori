"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useSession } from "@/lib/session";

/** "/" has nothing of its own: it sends the visitor to their first site, or to sign-in. */
export function Home() {
  const { status, me } = useSession();
  const router = useRouter();
  const firstSite = me?.sites[0]?.id;

  useEffect(() => {
    if (status === "anonymous") router.replace("/login");
    else if (status === "authenticated" && firstSite) router.replace(`/sites/${firstSite}`);
  }, [status, firstSite, router]);

  if (status === "authenticated" && !firstSite) {
    return (
      <main className="centered">
        <div className="card narrow">
          <h1>Tesis yok</h1>
          <p className="muted">Bu hesaba henüz bir tesis atanmamış. Bir yöneticiyle görüşün.</p>
        </div>
      </main>
    );
  }
  return (
    <main className="centered">
      <p className="muted">Yükleniyor…</p>
    </main>
  );
}
