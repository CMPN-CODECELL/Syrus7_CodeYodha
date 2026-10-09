"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  CloudLightning,
  Pause,
  RotateCcw,
  Swords,
  TrendingDown,
  TrendingUp,
  Waves,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Eyebrow } from "@/components/ui/panel";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { errorMessage } from "@/lib/errors";
import { useDemoActions, useDemoMode, useDemoState } from "@/lib/queries";
import { useUiStore } from "@/lib/store";
import type { BrokerFault, FeedScenario } from "@/lib/types";
import { cn } from "@/lib/utils";

const FEEDS: { id: FeedScenario; label: string; hint: string; icon: LucideIcon }[] = [
  { id: "calm", label: "Calm", hint: "Mean-reverting replay", icon: Waves },
  { id: "dip_to_trigger", label: "Dip to trigger", hint: "Walks down through an armed mandate", icon: TrendingDown },
  { id: "spike", label: "Spike", hint: "+4% jump, breaks approval bands", icon: TrendingUp },
  { id: "crash", label: "Crash", hint: "Whole market down about 6%", icon: CloudLightning },
  { id: "stale", label: "Stale feed", hint: "Ticks stop; approvals pause", icon: Pause },
];

const FAULTS: { id: BrokerFault; label: string; hint: string }[] = [
  { id: "none", label: "None", hint: "Broker behaves" },
  { id: "accept_then_drop", label: "Accept, drop ack", hint: "Executor must reconcile by tag, not retry" },
  { id: "delay_ack", label: "Delay ack", hint: "Slow acknowledgement" },
  { id: "reject", label: "Reject", hint: "RMS rejects the order" },
  { id: "partial_fill", label: "Partial fill", hint: "Half fills; plan stops" },
  { id: "connection_failure", label: "Connection fail", hint: "Broker unreachable" },
];

const SYMBOLS = ["TATAMOTORS", "RELIANCE", "INFY", "TCS", "HDFCBANK", "SBIN", "ITC", "NIFTY"];

export function PresenterControls() {
  const open = useUiStore((s) => s.presenterOpen);
  const setOpen = useUiStore((s) => s.setPresenterOpen);
  const demoMode = useDemoMode();
  const state = useDemoState(open);
  const { reset, feed, fault } = useDemoActions();
  const [symbol, setSymbol] = useState("TATAMOTORS");
  const [count, setCount] = useState(1);
  const [confirmReset, setConfirmReset] = useState(false);

  const onError = (title: string) => (e: unknown) => toast.error(title, { description: errorMessage(e) });

  // Ctrl+. (Cmd+. on a Mac) toggles the panel during a demo.
  useEffect(() => {
    if (!demoMode) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key === ".") {
        e.preventDefault();
        setOpen(!useUiStore.getState().presenterOpen);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [demoMode, setOpen]);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto sm:max-w-md">
        <SheetHeader className="border-b border-hairline">
          <SheetTitle>Demo controls</SheetTitle>
          <SheetDescription>
            Drive the replay feed and inject broker faults. Only available when the backend runs in demo mode.
          </SheetDescription>
          {state.data && (
            <div className="mt-2 flex flex-wrap gap-2 text-xs">
              <span className="rounded-md bg-surface-2 px-2 py-1 ring-1 ring-hairline">
                Feed: <span className="num font-medium text-foreground">{state.data.feed_mode}</span>
              </span>
              <span className="rounded-md bg-surface-2 px-2 py-1 ring-1 ring-hairline">
                Broker fault: <span className="num font-medium text-foreground">{state.data.broker_fault}</span>
              </span>
            </div>
          )}
        </SheetHeader>

        <div className="space-y-6 p-4">
          <section className="space-y-2">
            <Eyebrow>Market feed scenario</Eyebrow>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">Symbol</span>
              <Select value={symbol} onValueChange={(v) => setSymbol(String(v))}>
                <SelectTrigger size="sm" className="w-40">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SYMBOLS.map((s) => (
                    <SelectItem key={s} value={s}>
                      {s}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid grid-cols-1 gap-1.5">
              {FEEDS.map((f) => {
                const active = state.data?.feed_mode === f.id;
                return (
                  <button
                    key={f.id}
                    type="button"
                    disabled={feed.isPending}
                    onClick={() =>
                      feed.mutate(
                        { scenario: f.id, symbol },
                        {
                          onSuccess: () => toast.info(`Feed scenario: ${f.label}`, { description: `${f.hint} (${symbol}).` }),
                          onError: onError("Feed scenario did not start"),
                        },
                      )
                    }
                    className={cn(
                      "flex min-h-11 items-center gap-3 rounded-lg px-3 text-left ring-1 transition-colors disabled:opacity-60",
                      active ? "bg-info/10 ring-info/40" : "bg-surface-2 ring-hairline hover:ring-hairline-strong",
                    )}
                  >
                    <f.icon className={cn("size-4 shrink-0", active ? "text-info" : "text-muted-foreground")} />
                    <span className="flex flex-col">
                      <span className="text-sm font-medium">{f.label}</span>
                      <span className="text-xs text-muted-foreground">{f.hint}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          </section>

          <section className="space-y-2">
            <Eyebrow>Broker fault injection</Eyebrow>
            <div className="grid grid-cols-2 gap-1.5">
              {FAULTS.map((f) => {
                const active = state.data?.broker_fault === f.id;
                return (
                  <button
                    key={f.id}
                    type="button"
                    disabled={fault.isPending}
                    title={f.hint}
                    onClick={() =>
                      fault.mutate(
                        { mode: f.id, count },
                        {
                          onSuccess: () =>
                            toast.info(f.id === "none" ? "Broker faults cleared" : `Next ${count} order(s): ${f.label}`, { description: f.hint }),
                          onError: onError("Broker fault was not set"),
                        },
                      )
                    }
                    className={cn(
                      "flex min-h-11 flex-col justify-center rounded-lg px-3 py-1.5 text-left ring-1 transition-colors disabled:opacity-60",
                      active ? "bg-warning/10 ring-warning/40" : "bg-surface-2 ring-hairline hover:ring-hairline-strong",
                    )}
                  >
                    <span className="text-sm font-medium">{f.label}</span>
                    <span className="line-clamp-1 text-[11px] text-muted-foreground">{f.hint}</span>
                  </button>
                );
              })}
            </div>
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              Apply to the next
              <input
                type="number"
                min={1}
                max={10}
                value={count}
                onChange={(e) => setCount(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
                className="num h-8 w-14 rounded-md bg-surface-2 px-2 text-foreground ring-1 ring-hairline outline-none focus-visible:ring-ring"
              />
              order(s)
            </label>
          </section>

          <section className="space-y-2">
            <Eyebrow>Demo</Eyebrow>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" nativeButton={false} render={<Link href="/attack-lab" onClick={() => setOpen(false)} />}>
                <Swords /> Open Attack Lab
              </Button>
              <Button
                variant={confirmReset ? "destructive" : "outline"}
                disabled={reset.isPending}
                onClick={() => {
                  if (!confirmReset) {
                    setConfirmReset(true);
                    setTimeout(() => setConfirmReset(false), 4000);
                    return;
                  }
                  setConfirmReset(false);
                  reset.mutate(undefined, {
                    onSuccess: () => toast.success("Demo reset", { description: "Your paper account and feed are restored, your drafts and mandates cleared. The audit chain is kept." }),
                    onError: onError("Demo reset failed"),
                  });
                }}
              >
                <RotateCcw /> {confirmReset ? "Click again to reset" : "Reset demo"}
              </Button>
            </div>
            <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
              <Zap className="mt-0.5 size-3 shrink-0 text-warning" />
              Reset restores only your own paper account, sets the feed to calm and clears broker faults, and removes your proposals,
              mandates, plans and chat. Other accounts are untouched. The audit chain is append-only and is kept.
            </p>
          </section>
        </div>
      </SheetContent>
    </Sheet>
  );
}
