"use client";

import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { ServerCrash } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { onboardingComplete } from "@/lib/auth";
import { errorMessage, isApiError } from "@/lib/errors";
import { useMe } from "@/lib/queries";
import { KillSwitchBanner } from "./authority-strip";
import { BrandMark } from "./brand";
import { CommandPalette } from "./command-palette";
import { LiveBridge } from "./live-bridge";
import { MobileTabBar, NavRail } from "./nav-rail";
import { PresenterControls } from "./presenter-controls";
import { TopBar } from "./top-bar";

function ShellSkeleton({ label = "Loading Countersign" }: { label?: string }) {
  return (
    <div className="flex h-dvh flex-col" aria-busy="true" aria-label={label}>
      <div className="flex h-14 items-center gap-3 border-b border-hairline px-3 md:px-4">
        <BrandMark />
        <Skeleton className="hidden h-4 w-24 sm:block" />
        <Skeleton className="ml-auto size-9 md:w-60 lg:w-72" />
        <Skeleton className="size-9 sm:w-36" />
        <Skeleton className="size-9 rounded-full" />
      </div>
      <div className="flex flex-1">
        <div className="hidden w-14 border-r border-hairline md:block" />
        <div className="flex-1 space-y-3 p-6">
          <Skeleton className="h-8 w-1/3" />
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-32 w-2/3" />
        </div>
      </div>
    </div>
  );
}

function BackendDown({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <div className="grid min-h-dvh place-items-center bg-background p-6">
      <div className="panel w-full max-w-lg space-y-4 p-6">
        <div className="flex items-center gap-3">
          <span className="grid size-10 place-items-center rounded-xl bg-danger/10 ring-1 ring-danger/25">
            <ServerCrash className="size-5 text-danger" />
          </span>
          <div>
            <h1 className="text-base font-semibold">Cannot reach the Countersign API</h1>
            <p className="text-xs text-muted-foreground">{isApiError(error) ? error.code : "NETWORK_ERROR"}</p>
          </div>
        </div>
        <p className="text-sm text-muted-foreground">{errorMessage(error)}</p>
        <div className="rounded-lg bg-surface-2 p-3 font-mono text-xs leading-relaxed text-muted-foreground ring-1 ring-hairline">
          <p># start the backend (port 8000), or run the UI on fixtures:</p>
          <p className="text-foreground">NEXT_PUBLIC_MOCK=1 pnpm dev</p>
        </div>
        <Button onClick={onRetry}>Retry</Button>
      </div>
    </div>
  );
}

/** Where the visitor is now, so sign-in and onboarding can bring them straight back. */
function currentPath() {
  return `${window.location.pathname}${window.location.search}`;
}

/**
 * Auth gate plus the global shell: rail, top bar, live bridge. Signed-out visitors go to
 * /signin, accounts without an app PIN go to /onboarding, real outages show BackendDown.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const unauthenticated = isApiError(me.error) && (me.error.status === 401 || me.error.status === 403);
  const needsOnboarding = !!me.data && !me.isError && !onboardingComplete(me.data);

  useEffect(() => {
    if (unauthenticated) router.replace(`/signin?next=${encodeURIComponent(currentPath())}`);
    else if (needsOnboarding) router.replace(`/onboarding?next=${encodeURIComponent(currentPath())}`);
  }, [unauthenticated, needsOnboarding, router]);

  if (me.isPending) return <ShellSkeleton />;
  if (unauthenticated) return <ShellSkeleton label="Taking you to sign in" />;
  if (me.error) return <BackendDown error={me.error} onRetry={() => void me.refetch()} />;
  if (needsOnboarding) return <ShellSkeleton label="Finishing account setup" />;

  return (
    <div className="flex h-dvh flex-col overflow-hidden">
      <LiveBridge />
      <KillSwitchBanner />
      <TopBar me={me.data} />
      <div className="flex min-h-0 flex-1">
        <NavRail />
        <main id="main" className="relative min-h-0 min-w-0 flex-1 overflow-hidden pb-[calc(3.5rem+env(safe-area-inset-bottom))] md:pb-0">
          {children}
        </main>
      </div>
      <MobileTabBar />
      <CommandPalette />
      <PresenterControls />
    </div>
  );
}
