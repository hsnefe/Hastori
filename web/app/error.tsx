"use client";

// A render error in a page must not end in a blank screen or Next's English default.

export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="centered">
      <div className="card narrow" role="alert">
        <h1>Bir şeyler ters gitti</h1>
        <p className="muted">Sayfa gösterilemedi. Yeniden deneyebilir ya da sayfayı yenileyebilirsiniz.</p>
        <button type="button" className="primary" onClick={reset}>
          Tekrar dene
        </button>
      </div>
    </main>
  );
}
