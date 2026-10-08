import type { Metadata } from "next";

import { RulesPage } from "@/components/RulesPage";

export const metadata: Metadata = { title: "Kurallar" };

export default async function Page({ params }: { params: Promise<{ siteId: string }> }) {
  const { siteId } = await params;
  return <RulesPage siteId={siteId} />;
}
