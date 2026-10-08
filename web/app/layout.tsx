import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { Providers } from "@/components/Providers";

import "./globals.css";

// The CSP nonce is made per request (proxy.ts), so no page can be built ahead of time.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: { default: "Hastori", template: "%s · Hastori" },
  description: "Endüstriyel enerji ve cihaz izleme paneli",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light dark",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="tr" suppressHydrationWarning>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
