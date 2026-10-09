import { AppShell } from "@/components/shell/app-shell";

/** Signed-in application: copilot, portfolio, market, plans, mandates, outcomes, attack lab, security. */
export default function AppLayout({ children }: LayoutProps<"/">) {
  return <AppShell>{children}</AppShell>;
}
