import type { ReactNode } from "react";

import { SiteShell } from "@/components/SiteShell";

// The page skeleton is a server component; everything that needs the signed-in user, the socket
// or a chart is a client component inside it.
export default async function SiteLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ siteId: string }>;
}) {
  const { siteId } = await params;
  return <SiteShell siteId={siteId}>{children}</SiteShell>;
}
