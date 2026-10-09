import Link from "next/link";
import { AuthBackdrop } from "@/components/auth/auth-backdrop";
import { BrandMark } from "@/components/shell/brand";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { displayFont } from "@/lib/fonts";
import { cn } from "@/lib/utils";

/** Sign in, sign up and onboarding: a calm, centered frame outside the app shell. */
export default function AuthLayout({ children }: LayoutProps<"/">) {
  return (
    <div className={cn(displayFont.variable, "relative isolate flex min-h-dvh flex-col overflow-x-clip bg-background")}>
      <AuthBackdrop />
      <header className="pt-safe relative z-10 mx-auto flex h-16 w-full max-w-6xl items-center justify-between px-4 md:px-8">
        <Link
          href="/"
          aria-label="Countersign home"
          className="-ml-1 flex min-h-11 items-center gap-2.5 rounded-lg px-1 outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <BrandMark />
          <span className="text-[15px] font-semibold tracking-tight">Countersign</span>
        </Link>
        <div className="flex items-center gap-1">
          <Link
            href="/"
            className="hidden min-h-11 items-center rounded-lg px-3 text-sm text-muted-foreground transition-colors outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring sm:inline-flex"
          >
            Back to home
          </Link>
          <ThemeToggle size="icon-lg" />
        </div>
      </header>
      <main id="main" className="relative z-10 flex flex-1 justify-center px-4 pt-4 pb-10 sm:items-center sm:pt-2">
        {children}
      </main>
      <footer className="relative z-10 px-4 pb-6 text-center text-xs text-subtle">
        Paper trading on a simulated market. No real money, no real orders.
      </footer>
    </div>
  );
}
