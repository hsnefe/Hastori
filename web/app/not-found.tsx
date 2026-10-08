import Link from "next/link";

export default function NotFound() {
  return (
    <main className="centered">
      <div className="card narrow">
        <h1>Sayfa bulunamadı</h1>
        <p className="muted">Aradığınız sayfa yok ya da taşınmış.</p>
        <Link className="button" href="/">
          Ana sayfaya dön
        </Link>
      </div>
    </main>
  );
}
