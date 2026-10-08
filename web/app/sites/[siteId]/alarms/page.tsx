import type { Metadata } from "next";
import { Suspense } from "react";

import { AlarmsPage } from "@/components/AlarmsPage";

export const metadata: Metadata = { title: "Alarmlar" };

export default async function Page({ params }: { params: Promise<{ siteId: string }> }) {
  const { siteId } = await params;
  return (
    <Suspense fallback={<p className="muted">Yükleniyor…</p>}>
      <AlarmsPage siteId={siteId} />
    </Suspense>
  );
}
