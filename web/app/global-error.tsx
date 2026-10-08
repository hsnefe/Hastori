"use client";

// The last resort: an error in the root layout itself. It replaces the whole document, so it
// brings its own <html> and no stylesheet of the app.

export default function GlobalError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <html lang="tr">
      <body style={{ fontFamily: "system-ui, sans-serif", padding: "2rem" }}>
        <h1>Bir şeyler ters gitti</h1>
        <p>Uygulama başlatılamadı.</p>
        <button type="button" onClick={reset}>
          Tekrar dene
        </button>
      </body>
    </html>
  );
}
