import type { Metadata } from "next";

export const metadata: Metadata = { title: "Panel" };

// The dashboard itself comes with the live-data package.
export default function Page() {
  return <p className="muted">Panel yakında.</p>;
}
