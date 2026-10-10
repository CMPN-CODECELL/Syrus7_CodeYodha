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

export interface PromptLevel {
  level: "L1" | "L2" | "L3" | "L4";
  title: string;
  hint: string;
  /** read: answered from the read-only gateway. draft: becomes a proposal that waits for your countersignature. */
  mode: "read" | "draft";
  items: string[];
}

/** The official 021-01 examples, by level. */
export const PROMPT_LEVELS: PromptLevel[] = [
  {
    level: "L1",
    title: "Ask",
    hint: "Read only: your account and the market",
    mode: "read",
    items: [
      "What's my P&L today?",
      "Which positions are down more than 5%?",
      "What did I pay on average for INFY?",
      "Show NIFTY options near the money for this expiry",
    ],
  },
  {
    level: "L2",
    title: "Place, modify, cancel",
    hint: "Drafted exactly, sent only after you countersign",
    mode: "draft",
    items: ["Buy 5 TCS at market and buy 5 INFY at market", "Sell half my TCS", "Move my stop-loss on HDFC Bank up to 1640", "Cancel all my open orders"],
  },
  {
    level: "L3",
    title: "Standing instructions",
    hint: "Watched against live prices, fire once",
    mode: "draft",
    items: ["If Reliance drops below 2800, buy 10", "Tell me when any holding falls 3% in a day"],
  },
  {
    level: "L4",
    title: "Plans",
    hint: "Several orders, one signature, each result reported",
    mode: "draft",
    items: ["Exit all my losing intraday positions", "Rebalance so no single stock is more than 20% of my portfolio"],
  },
];

export const SUGGESTED_PROMPTS = {
  read: PROMPT_LEVELS.filter((l) => l.mode === "read").flatMap((l) => l.items),
  propose: PROMPT_LEVELS.filter((l) => l.mode === "draft").flatMap((l) => l.items),
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
        {PROMPT_LEVELS.map((lvl) => (
          <CommandGroup key={lvl.level} heading={`${lvl.level} · ${lvl.title}${lvl.mode === "read" ? " (read only)" : " (drafted for countersignature)"}`}>
            {lvl.items.map((p) => (
              <CommandItem
                key={p}
                value={`${lvl.mode === "read" ? "ask" : "draft"} ${lvl.level} ${p}`}
                onSelect={() =>
                  run(() => {
                    router.push("/copilot");
                    requestComposer(p, { send: true });
                  })
                }
              >
                {lvl.mode === "read" ? <MessageSquare className="text-ai" /> : <PenLine className="text-human" />}
                {p}
              </CommandItem>
            ))}
          </CommandGroup>
        ))}
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
