import type { Metadata } from "next";

import { Dashboard } from "@/components/Dashboard";

export const metadata: Metadata = { title: "Panel" };

export default async function Page({ params }: { params: Promise<{ siteId: string }> }) {
  const { siteId } = await params;
  return <Dashboard key={siteId} siteId={siteId} />;
}
