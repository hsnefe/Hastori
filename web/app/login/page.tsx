import type { Metadata } from "next";

import { LoginForm } from "@/components/LoginForm";

export const metadata: Metadata = { title: "Giriş" };

export default function Page() {
  return <LoginForm />;
}
