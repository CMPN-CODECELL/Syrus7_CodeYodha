"use client";

import Link from "next/link";
import { useSyncExternalStore } from "react";
import { Landmark, LogOut, MessageCircle, Monitor, Moon, Presentation, Search, ShieldCheck, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { BrokerMenuLine } from "@/components/broker/broker-connection";
import { Kbd } from "@/components/ui/kbd";
import { IS_MOCK } from "@/lib/api";
import { useActiveBroker, useDemoMode } from "@/lib/queries";
import { useUiStore } from "@/lib/store";
import type { Me } from "@/lib/types";
import { BrandMark } from "./brand";
import { SystemStatus } from "./status-indicators";
import { useSignOut } from "./use-sign-out";

export { ThemeToggle } from "./theme-toggle";

export function useIsMac() {
  return useSyncExternalStore(
    () => () => {},
    () => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent),
    () => false,
  );
}

function initials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] ?? "?").slice(0, 2);
  return letters.toUpperCase();
}

function UserMenu({ me }: { me: Me }) {
  const { theme, setTheme } = useTheme();
  const setPresenterOpen = useUiStore((s) => s.setPresenterOpen);
  const demoMode = useDemoMode();
  const isMac = useIsMac();
  const { signOut, pending } = useSignOut();
  const broker = useActiveBroker();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <button
            type="button"
            className="grid size-9 shrink-0 place-items-center rounded-full bg-human/15 text-[12px] font-semibold text-human ring-1 ring-human/30 transition-colors outline-none hover:bg-human/25 focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={`Account menu for ${me.user.display_name}`}
          />
        }
      >
        {initials(me.user.display_name)}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={8} className="w-64">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="px-2 py-2">
            <div className="flex min-w-0 flex-col gap-0.5">
              <span className="truncate text-sm font-medium text-foreground">{me.user.display_name}</span>
              {me.email && <span className="truncate text-xs font-normal text-muted-foreground">{me.email}</span>}
              <BrokerMenuLine me={me} />
            </div>
          </DropdownMenuLabel>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem render={<Link href="/security#broker" />}>
          <Landmark /> Broker connection
          <DropdownMenuShortcut className="tracking-normal">{broker.kind === "021" ? (broker.live021 ? "021 live" : "021") : "paper"}</DropdownMenuShortcut>
        </DropdownMenuItem>
        <DropdownMenuItem render={<Link href="/security" />}>
          <ShieldCheck /> Security Center
        </DropdownMenuItem>
        <DropdownMenuItem render={<Link href="/alerts" />}>
          <MessageCircle /> WhatsApp alerts
          <DropdownMenuShortcut className="tracking-normal">read only</DropdownMenuShortcut>
        </DropdownMenuItem>
        {demoMode && (
          <DropdownMenuItem onClick={() => setPresenterOpen(true)}>
            <Presentation /> Demo controls
            <DropdownMenuShortcut className="hidden tracking-normal md:inline">{isMac ? "⌘ ." : "Ctrl+."}</DropdownMenuShortcut>
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuLabel>Theme</DropdownMenuLabel>
          <DropdownMenuRadioGroup value={theme ?? "dark"} onValueChange={(v) => setTheme(String(v))}>
            <DropdownMenuRadioItem value="light" closeOnClick={false}>
              <Sun /> Light
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem value="dark" closeOnClick={false}>
              <Moon /> Dark
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem value="system" closeOnClick={false}>
              <Monitor /> System
            </DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => void signOut()} variant="destructive" disabled={pending}>
          <LogOut /> {pending ? "Signing out..." : "Sign out"}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Brand, search, one system status control and the account menu. Nothing else lives here. */
export function TopBar({ me }: { me: Me }) {
  const setPaletteOpen = useUiStore((s) => s.setPaletteOpen);
  const isMac = useIsMac();
  return (
    <header className="pt-safe flex h-14 shrink-0 items-center gap-2 border-b border-hairline bg-surface-1/80 px-3 backdrop-blur md:px-4">
      <Link
        href="/"
        aria-label="Countersign home"
        className="flex min-h-9 items-center gap-2 rounded-lg pr-1 outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <BrandMark />
        <span className="hidden text-[15px] font-semibold tracking-tight sm:inline">Countersign</span>
      </Link>
      {IS_MOCK && (
        <span className="hidden h-5 items-center rounded px-1.5 text-[10px] font-semibold tracking-wider text-warning uppercase ring-1 ring-warning/30 ring-inset md:inline-flex">
          Mock data
        </span>
      )}

      <div className="ml-auto flex items-center gap-1.5 sm:gap-2">
        <button
          type="button"
          onClick={() => setPaletteOpen(true)}
          className="flex size-9 items-center justify-center gap-2 rounded-lg text-xs text-muted-foreground ring-1 ring-hairline transition-colors outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring md:w-60 md:justify-start md:bg-surface-2/70 md:px-2.5 lg:w-72"
          aria-label="Search and commands"
          aria-keyshortcuts={isMac ? "Meta+K" : "Control+K"}
        >
          <Search className="size-4 md:size-3.5" aria-hidden />
          <span className="hidden md:inline">Search or jump to...</span>
          <Kbd className="ml-auto hidden md:inline-flex">{isMac ? "⌘K" : "Ctrl K"}</Kbd>
        </button>
        <SystemStatus />
        <UserMenu me={me} />
      </div>
    </header>
  );
}
