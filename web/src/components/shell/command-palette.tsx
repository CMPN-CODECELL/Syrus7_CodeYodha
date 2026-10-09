"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { Lock, LogOut, MessageSquare, Moon, PenLine, Power, Presentation, Swords } from "lucide-react";
import { useTheme } from "next-themes";
import { toast } from "sonner";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/errors";
import { useDemoMode, useKillSwitch } from "@/lib/queries";
import { useChatStore, useUiStore } from "@/lib/store";
import type { ProposalStatus } from "@/lib/types";
import { NAV } from "./nav";
import { useSignOut } from "./use-sign-out";

export const SUGGESTED_PROMPTS = {
  read: [
    "What is my P&L today?",
    "Which positions are down more than 5%?",
    "What is my average purchase price for Reliance?",
    "Show NIFTY options near the money",
  ],
  propose: [
    "Buy 10 RELIANCE at 2800",
    "Alert me if TATAMOTORS falls more than 5%",
    "Sell 20 INFY and buy 5 TCS",
    "Buy 100 TATAMOTORS if price falls below 900",
  ],
};

/** Global Cmd/Ctrl+K palette: navigation, copilot prompts and demo actions. */
export function CommandPalette() {
  const router = useRouter();
  const open = useUiStore((s) => s.paletteOpen);
  const setOpen = useUiStore((s) => s.setPaletteOpen);
  const requestComposer = useUiStore((s) => s.requestComposer);
  const setPresenterOpen = useUiStore((s) => s.setPresenterOpen);
  const requestAttackRun = useUiStore((s) => s.requestAttackRun);
  const inject = useChatStore((s) => s.inject);
  const demoMode = useDemoMode();
  const kill = useKillSwitch();
  const { signOut } = useSignOut();
  const { resolvedTheme, setTheme } = useTheme();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(!useUiStore.getState().paletteOpen);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setOpen]);

  const run = (fn: () => void) => {
    setOpen(false);
    fn();
  };

  const showProposal = async (status: ProposalStatus, title: string) => {
    try {
      const [p] = await api.proposals({ status, limit: 1 });
      if (!p) {
        toast.info(`No ${status.toLowerCase().replace(/_/g, " ")} proposal yet`, { description: "Ask the copilot to draft one first." });
        return;
      }
      router.push("/copilot");
      inject([{ type: "card", card: { kind: "proposal", proposal_id: p.id, status: p.status }, key: "" }], title);
    } catch (e) {
      toast.error("Could not load the proposal", { description: errorMessage(e) });
    }
  };

  return (
    <CommandDialog open={open} onOpenChange={setOpen} title="Command palette" description="Navigate, ask the copilot, or run a demo action">
      <Command loop>
      <CommandInput placeholder="Jump to a page, ask the copilot, or run an action..." />
      <CommandList className="max-h-[min(60dvh,420px)]">
        <CommandEmpty>No matching command.</CommandEmpty>
        <CommandGroup heading="Navigate">
          {NAV.map((n) => (
            <CommandItem key={n.href} value={`go ${n.label} ${n.description}`} onSelect={() => run(() => router.push(n.href))}>
              <n.icon />
              <span>{n.label}</span>
              <span className="ml-2 truncate text-xs text-muted-foreground">{n.description}</span>
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Ask the copilot (read only)">
          {SUGGESTED_PROMPTS.read.map((p) => (
            <CommandItem
              key={p}
              value={`ask ${p}`}
              onSelect={() =>
                run(() => {
                  router.push("/copilot");
                  requestComposer(p, { send: true });
                })
              }
            >
              <MessageSquare className="text-ai" />
              {p}
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandGroup heading="Draft for countersignature">
          {SUGGESTED_PROMPTS.propose.map((p) => (
            <CommandItem
              key={p}
              value={`draft ${p}`}
              onSelect={() =>
                run(() => {
                  router.push("/copilot");
                  requestComposer(p, { send: true });
                })
              }
            >
              <PenLine className="text-human" />
              {p}
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Demo">
          <CommandItem value="show blocked proposal misread 100 1000" onSelect={() => run(() => void showProposal("BLOCKED", "Here is the most recent proposal the verifier blocked:"))}>
            <Lock className="text-danger" />
            Show latest blocked proposal (AI misread)
          </CommandItem>
          <CommandItem value="show awaiting approval order countersign" onSelect={() => run(() => void showProposal("AWAITING_APPROVAL", "This proposal is waiting for your countersignature:"))}>
            <PenLine className="text-human" />
            Show latest proposal awaiting countersignature
          </CommandItem>
          <CommandItem
            value="run all attacks attack lab"
            onSelect={() =>
              run(() => {
                router.push("/attack-lab");
                requestAttackRun();
              })
            }
          >
            <Swords />
            Run all attacks in the Attack Lab
          </CommandItem>
          {demoMode && (
            <CommandItem value="demo controls presenter reset feed broker fault" onSelect={() => run(() => setPresenterOpen(true))}>
              <Presentation />
              Open demo controls
              <CommandShortcut>Ctrl+.</CommandShortcut>
            </CommandItem>
          )}
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="System">
          <CommandItem value="toggle theme dark light" onSelect={() => run(() => setTheme(resolvedTheme === "dark" ? "light" : "dark"))}>
            <Moon />
            Toggle light and dark theme
          </CommandItem>
          <CommandItem
            value="engage kill switch halt"
            onSelect={() =>
              run(() =>
                kill.mutate(
                  { engaged: true },
                  {
                    onSuccess: () => toast.error("Kill switch engaged", { description: "All approvals and executions are halted. Disengaging requires your PIN." }),
                    onError: (e) => toast.error("Kill switch did not engage", { description: errorMessage(e) }),
                  },
                ),
              )
            }
          >
            <Power className="text-danger" />
            Engage kill switch
            <CommandShortcut>instant</CommandShortcut>
          </CommandItem>
          <CommandItem value="sign out log out" onSelect={() => run(() => void signOut())}>
            <LogOut />
            Sign out
          </CommandItem>
        </CommandGroup>
      </CommandList>
      </Command>
    </CommandDialog>
  );
}
