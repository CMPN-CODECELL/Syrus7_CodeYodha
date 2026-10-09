"use client";

import { cn } from "@/lib/utils";
import { usePublicStatus } from "./hooks";

/** Footer health line, driven by the same public status poll as the live panel. */
export function FooterStatus() {
  const { state } = usePublicStatus();
  const look = {
    loading: { dot: "bg-muted-foreground", text: "Checking system status" },
    ok: { dot: "bg-profit", text: "All systems operational" },
    degraded: { dot: "bg-warning", text: "Degraded: some services are down" },
    unavailable: { dot: "bg-muted-foreground/60", text: "Status unavailable" },
  }[state];
  return (
    <a href="#security" className="inline-flex w-fit items-center gap-2.5 rounded-sm text-[14px] font-medium focus-visible:outline-2 focus-visible:outline-ring">
      <span className="relative flex size-2.5">
        {state === "ok" && <span className={cn("lp-ping absolute inset-0 rounded-full", look.dot)} />}
        <span className={cn("relative size-2.5 rounded-full", look.dot)} />
      </span>
      <span className={state === "ok" ? "text-foreground" : "text-muted-foreground"}>{look.text}</span>
    </a>
  );
}
