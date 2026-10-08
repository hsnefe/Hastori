"use client";

import { useSession } from "@/lib/session";

/** The session could not be checked because the server did not answer: not a sign-out. */
export function Unavailable() {
  const { retry } = useSession();
  return (
    <main className="centered">
      <div className="card narrow" role="alert">
        <h1>Sunucuya ulaşılamıyor</h1>
        <p className="muted">Oturumunuz kapanmadı; sunucu şu an yanıt vermiyor.</p>
        <button type="button" className="primary" onClick={retry}>
          Tekrar dene
        </button>
      </div>
    </main>
  );
}
