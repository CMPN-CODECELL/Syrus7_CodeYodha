"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useSyncExternalStore } from "react";
import { cn } from "@/lib/utils";
import { istTime } from "./format";
import { useIsClient } from "./hooks";

// ---------------------------------------------------------------------------
// IST clock: one shared one-second ticker for every clock on the page.

let nowMs = 0;
const clockListeners = new Set<() => void>();
let clockTimer: number | null = null;

function subscribeClock(onChange: () => void) {
  clockListeners.add(onChange);
  if (clockTimer === null) {
    nowMs = Date.now();
    clockTimer = window.setInterval(() => {
      nowMs = Date.now();
      clockListeners.forEach((l) => l());
    }, 1000);
  }
  return () => {
    clockListeners.delete(onChange);
    if (clockListeners.size === 0 && clockTimer !== null) {
      window.clearInterval(clockTimer);
      clockTimer = null;
    }
  };
}

export function IstClock({ className }: { className?: string }) {
  const now = useSyncExternalStore(
    subscribeClock,
    () => nowMs,
    () => 0,
  );
  return (
    <span className={cn("num text-[12px] tracking-[0.14em] text-muted-foreground tabular-nums", className)}>
      <span className="text-subtle">IST </span>
      {now ? istTime(now) : "--:--:--"}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Theme controls

/** Single icon button that flips between dark and light. Icons swap through CSS, so SSR matches. */
export function ThemeToggle({ className }: { className?: string }) {
  const { resolvedTheme, setTheme } = useTheme();
  return (
    <button
      type="button"
      onClick={() => setTheme(resolvedTheme === "light" ? "dark" : "light")}
      aria-label="Switch between dark and light theme"
      className={cn(
        "inline-flex size-10 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-foreground/[0.06] hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring",
        className,
      )}
    >
      <Moon className="size-[18px] dark:hidden" aria-hidden />
      <Sun className="hidden size-[18px] dark:block" aria-hidden />
    </button>
  );
}

/** Three-way light / dark / system switch for the footer. */
export function ThemeSwitch({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const isClient = useIsClient();
  const options = [
    { value: "system", label: "Match system theme", icon: Monitor },
    { value: "light", label: "Light theme", icon: Sun },
    { value: "dark", label: "Dark theme", icon: Moon },
  ] as const;
  return (
    <div role="group" aria-label="Theme" className={cn("inline-flex items-center gap-0.5 rounded-full border border-hairline bg-surface-1/60 p-1", className)}>
      {options.map((o) => {
        const active = isClient && theme === o.value;
        return (
          <button
            key={o.value}
            type="button"
            aria-label={o.label}
            aria-pressed={isClient ? active : undefined}
            onClick={() => setTheme(o.value)}
            className={cn(
              "inline-flex size-8 items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring",
              active && "bg-foreground/[0.09] text-foreground",
            )}
          >
            <o.icon className="size-3.5" aria-hidden />
          </button>
        );
      })}
    </div>
  );
}
