"use client";

import { ArrowRight, Menu } from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type MouseEvent } from "react";
import { BrandMark } from "@/components/shell/brand";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { IstClock, ThemeToggle } from "./controls";
import { useLandingSession, usePublicStatus, useReducedMotion } from "./hooks";
import { arrowNudge } from "./styles";

/** Where the public market data comes from, from the status feed. Hidden until the first answer. */
function MarketPill({ className }: { className?: string }) {
  const { data } = usePublicStatus();
  if (!data?.market) return null;
  return (
    <span className={cn("rounded-full border border-hairline-strong px-2 py-0.5 text-[10.5px] font-medium tracking-wide text-muted-foreground", className)}>
      {data.market.mode === "live" ? "021 sandbox feed" : "Replay market"}
    </span>
  );
}

export const NAV_LINKS = [
  { id: "product", label: "Product" },
  { id: "how-it-works", label: "How it works" },
  { id: "security", label: "Security" },
  { id: "attack-lab", label: "Attack Lab" },
  { id: "faq", label: "FAQ" },
] as const;

type NavId = (typeof NAV_LINKS)[number]["id"];

const NAV_IDS = new Set<string>(NAV_LINKS.map((l) => l.id));

/**
 * Scroll spy: the last section (of every section with an id, nav item or not) whose top has passed a line 35% down
 * the viewport. Inside a section that has no nav item (Proof, Device biometrics) nothing is highlighted.
 */
function useActiveSection(): NavId | null {
  const [active, setActive] = useState<NavId | null>(null);
  useEffect(() => {
    let raf = 0;
    const measure = () => {
      raf = 0;
      const line = window.innerHeight * 0.35;
      let current: string | null = null;
      for (const el of document.querySelectorAll<HTMLElement>("main section[id]")) {
        if (el.getBoundingClientRect().top <= line) current = el.id;
      }
      setActive(current && NAV_IDS.has(current) ? (current as NavId) : null);
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(measure);
    };
    schedule();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, []);
  return active;
}

/** Smooth-scrolls to an in-page section and moves focus there for keyboard and screen reader users. */
export function useSectionJump() {
  const reduced = useReducedMotion();
  return (e: MouseEvent<HTMLAnchorElement>, id: string, delayMs = 0) => {
    const el = document.getElementById(id);
    if (!el) return;
    e.preventDefault();
    const go = () => {
      el.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
      el.focus({ preventScroll: true });
      history.replaceState(null, "", `#${id}`);
    };
    // From the mobile sheet, wait for it to close and release its scroll lock first.
    if (delayMs) window.setTimeout(go, delayMs);
    else go();
  };
}

const cell = "flex items-center border-l border-hairline";

export function SiteNav() {
  const active = useActiveSection();
  const jump = useSectionJump();
  const { signedIn, firstName, pending } = useLandingSession();
  const [open, setOpen] = useState(false);

  return (
    <header className="sticky top-0 z-50 border-b border-hairline bg-background/75 backdrop-blur-xl backdrop-saturate-150">
      <div className="mx-auto flex h-16 max-w-[1440px] items-stretch">
        <Link
          href="/"
          className="flex items-center gap-2.5 pr-4 pl-4 sm:pr-6 sm:pl-6 lg:border-r lg:border-hairline"
          aria-label="Countersign home"
        >
          <BrandMark className="size-8" />
          <span className="text-[16px] font-semibold tracking-tight">Countersign</span>
        </Link>

        <nav aria-label="Sections" className="hidden items-stretch lg:flex">
          {NAV_LINKS.map((l) => (
            <a
              key={l.id}
              href={`#${l.id}`}
              onClick={(e) => jump(e, l.id)}
              aria-current={active === l.id ? "location" : undefined}
              data-active={active === l.id}
              className="group/nav relative flex items-center border-r border-hairline px-4 text-[14px] font-medium text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring data-[active=true]:text-foreground xl:px-6"
            >
              {l.label}
              <span
                aria-hidden
                className="absolute inset-x-4 bottom-[-1px] h-px origin-left scale-x-0 bg-human transition-transform duration-300 group-data-[active=true]/nav:scale-x-100 xl:inset-x-6"
              />
            </a>
          ))}
        </nav>

        <div className="ml-auto flex items-stretch">
          <div className={cn(cell, "hidden gap-2.5 px-5 xl:flex")}>
            <IstClock />
            <MarketPill />
          </div>
          <div className={cn(cell, "px-1.5 sm:px-2")}>
            <ThemeToggle />
          </div>
          {pending ? (
            // Session not known yet: hold the space instead of flashing "Sign in" at a signed-in visitor.
            <span aria-hidden className="hidden w-[208px] sm:block" />
          ) : signedIn ? (
            <>
              <span className={cn(cell, "hidden px-5 text-sm text-muted-foreground md:flex")}>
                Hi,&nbsp;<span className="font-medium text-foreground">{firstName}</span>
              </span>
              <Link
                href="/copilot"
                className="group/btn hidden items-center gap-2 bg-foreground px-6 text-[15px] font-medium text-background transition-colors hover:bg-foreground/90 focus-visible:outline-2 focus-visible:-outline-offset-4 focus-visible:outline-background sm:flex"
              >
                Open Copilot
                <ArrowRight className={arrowNudge} aria-hidden />
              </Link>
            </>
          ) : (
            <>
              <Link
                href="/signin"
                className={cn(cell, "hidden px-5 text-[15px] font-medium text-foreground/90 transition-colors hover:bg-foreground/[0.04] hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring sm:flex")}
              >
                Sign in
              </Link>
              <Link
                href="/signup?next=/copilot"
                className="group/btn hidden items-center gap-2 bg-foreground px-6 text-[15px] font-medium text-background transition-colors hover:bg-foreground/90 focus-visible:outline-2 focus-visible:-outline-offset-4 focus-visible:outline-background sm:flex"
              >
                Get started
                <ArrowRight className={arrowNudge} aria-hidden />
              </Link>
            </>
          )}

          <Sheet open={open} onOpenChange={setOpen}>
            <SheetTrigger
              render={
                <button
                  type="button"
                  aria-label="Open menu"
                  className={cn(cell, "justify-center px-4 text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring lg:hidden")}
                />
              }
            >
              <Menu className="size-5" aria-hidden />
            </SheetTrigger>
            <SheetContent side="right" className="w-[86vw] max-w-sm gap-0 p-0">
              <div className="flex h-16 items-center gap-2.5 border-b border-hairline px-5">
                <BrandMark />
                <SheetTitle className="text-[15px] font-semibold">Countersign</SheetTitle>
              </div>
              <SheetDescription className="sr-only">Jump to a section or sign in.</SheetDescription>
              <nav aria-label="Sections" className="flex flex-col p-2">
                {NAV_LINKS.map((l) => (
                  <a
                    key={l.id}
                    href={`#${l.id}`}
                    onClick={(e) => {
                      setOpen(false);
                      jump(e, l.id, 280);
                    }}
                    aria-current={active === l.id ? "location" : undefined}
                    className={cn(
                      "flex h-12 items-center justify-between rounded-lg px-3 text-[16px] text-muted-foreground transition-colors hover:bg-foreground/[0.05] hover:text-foreground",
                      active === l.id && "text-foreground",
                    )}
                  >
                    {l.label}
                    {active === l.id && <span className="size-1.5 rounded-full bg-human" aria-hidden />}
                  </a>
                ))}
              </nav>
              <div className="mt-auto space-y-3 border-t border-hairline p-5">
                <div className="flex items-center justify-between">
                  <IstClock />
                  <MarketPill className="font-normal tracking-normal" />
                </div>
                {pending ? null : signedIn ? (
                  <SheetClose
                    nativeButton={false}
                    render={
                      <Link href="/copilot" className="flex h-12 items-center justify-center gap-2 rounded-lg bg-foreground text-[15px] font-medium text-background" />
                    }
                  >
                    Open Copilot <ArrowRight className="size-4" aria-hidden />
                  </SheetClose>
                ) : (
                  <div className="grid grid-cols-2 gap-2">
                    <SheetClose
                      nativeButton={false}
                      render={
                        <Link
                          href="/signin"
                          className="flex h-12 items-center justify-center rounded-lg border border-hairline-strong text-[15px] font-medium"
                        />
                      }
                    >
                      Sign in
                    </SheetClose>
                    <SheetClose
                      nativeButton={false}
                      render={
                        <Link
                          href="/signup?next=/copilot"
                          className="flex h-12 items-center justify-center rounded-lg bg-foreground text-[15px] font-medium text-background"
                        />
                      }
                    >
                      Get started
                    </SheetClose>
                  </div>
                )}
              </div>
            </SheetContent>
          </Sheet>
        </div>
      </div>
    </header>
  );
}
