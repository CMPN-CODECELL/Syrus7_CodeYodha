"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Ellipsis } from "lucide-react";
import { useState } from "react";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { NAV, isActive } from "./nav";

/** Desktop icon rail. Expands on hover/focus to show labels without shifting the layout. */
export function NavRail() {
  const pathname = usePathname();
  return (
    <nav
      aria-label="Primary"
      className="group/rail relative z-30 hidden w-14 shrink-0 md:block"
    >
      <div className="absolute inset-y-0 left-0 flex w-14 flex-col gap-1 overflow-hidden border-r border-hairline bg-surface-1/95 px-2 py-3 backdrop-blur transition-[width,box-shadow] duration-200 ease-out group-hover/rail:w-56 group-hover/rail:shadow-2xl group-hover/rail:shadow-black/30 group-has-[:focus-visible]/rail:w-56">
        {NAV.map((item) => {
          const active = isActive(pathname, item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "relative flex h-10 min-w-0 items-center gap-3 rounded-lg px-[11px] text-sm transition-colors outline-none",
                "focus-visible:ring-2 focus-visible:ring-ring",
                active ? "bg-human/10 text-foreground" : "text-muted-foreground hover:bg-foreground/[0.05] hover:text-foreground",
              )}
            >
              {active && <span className="absolute top-2 bottom-2 -left-2 w-[3px] rounded-r bg-human" aria-hidden />}
              <item.icon className={cn("size-[18px] shrink-0", active && "text-human")} aria-hidden />
              <span className="flex min-w-0 flex-col opacity-0 transition-opacity duration-150 group-hover/rail:opacity-100 group-has-[:focus-visible]/rail:opacity-100">
                <span className="truncate leading-tight font-medium">{item.label}</span>
                <span className="truncate text-[11px] leading-tight text-subtle">{item.description}</span>
              </span>
            </Link>
          );
        })}
      </div>
    </nav>
  );
}

/** Mobile bottom tab bar with the four primary areas and a "More" sheet. */
export function MobileTabBar() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const primary = NAV.slice(0, 4);
  const rest = NAV.slice(4);
  const restActive = rest.some((n) => isActive(pathname, n.href));
  return (
    <>
      <nav
        aria-label="Primary"
        className="pb-safe fixed inset-x-0 bottom-0 z-40 border-t border-hairline bg-surface-1/95 backdrop-blur md:hidden"
      >
        <div className="grid grid-cols-5">
          {primary.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn("flex h-14 flex-col items-center justify-center gap-0.5 text-[10.5px]", active ? "text-human" : "text-muted-foreground")}
              >
                <item.icon className="size-5" aria-hidden />
                {item.short}
              </Link>
            );
          })}
          <button
            type="button"
            onClick={() => setOpen(true)}
            className={cn("flex h-14 flex-col items-center justify-center gap-0.5 text-[10.5px]", restActive ? "text-human" : "text-muted-foreground")}
          >
            <Ellipsis className="size-5" aria-hidden />
            More
          </button>
        </div>
      </nav>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="bottom" className="pb-safe rounded-t-2xl">
          <SheetHeader>
            <SheetTitle>More</SheetTitle>
          </SheetHeader>
          <div className="grid grid-cols-2 gap-2 px-4 pb-4">
            {rest.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                onClick={() => setOpen(false)}
                className={cn(
                  "flex min-h-14 items-center gap-3 rounded-xl px-3 ring-1 ring-hairline",
                  isActive(pathname, item.href) ? "bg-human/10 text-foreground" : "bg-surface-2 text-muted-foreground",
                )}
              >
                <item.icon className="size-5 shrink-0" aria-hidden />
                <span className="flex flex-col">
                  <span className="text-sm font-medium text-foreground">{item.label}</span>
                  <span className="text-[11px]">{item.description}</span>
                </span>
              </Link>
            ))}
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
