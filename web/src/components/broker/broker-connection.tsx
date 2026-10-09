"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowLeftRight, CircleAlert, FlaskConical, Info, Landmark, type LucideIcon } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Chip, Dot } from "@/components/ui/chip";
import { Panel } from "@/components/ui/panel";
import { ErrorState, SkeletonRows } from "@/components/ui/states";
import { accountId } from "@/lib/auth";
import { brokerName, displayAccountId } from "@/lib/broker";
import { errorMessage } from "@/lib/errors";
import { dateTime, until } from "@/lib/format";
import { useActiveBroker, useSecurityStatus, useSetActiveBroker } from "@/lib/queries";
import { Disconnect021, Link021Card, maskedOf } from "./link-021";
import type { Tone } from "@/lib/status";
import type { BrokerAccount, BrokerKind, Me } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";

export interface BrokerHealth {
  tone: Tone;
  label: string;
}

/** One verdict per account: connected, connected with an error, token expired, or not connected. */
export function brokerHealth(a: BrokerAccount, now: number): BrokerHealth {
  if (!a.status.connected) return { tone: "danger", label: "Not connected" };
  const exp = a.status.token_expires_at ? Date.parse(a.status.token_expires_at) : NaN;
  if (a.kind === "021" && now && Number.isFinite(exp) && exp <= now) return { tone: "warning", label: "Token expired" };
  if (a.status.last_error) return { tone: "warning", label: "Connected, last call failed" };
  return { tone: "profit", label: "Connected" };
}

export function marketDataLabel(kind: BrokerKind, source: "live" | "replay"): string {
  if (source === "live") return kind === "021" ? "Live 021 feed" : "Live feed";
  return "Replay";
}

/** The backend label without an unmasked account id in it. */
function safeLabel(a: BrokerAccount): string | null {
  const label = a.label?.trim();
  if (!label || label === brokerName(a.kind)) return null;
  return a.account_id ? label.split(a.account_id).join(displayAccountId(a.account_id, a.kind)) : label;
}

const KIND_ICON: Record<BrokerKind, LucideIcon> = { "021": Landmark, paper: FlaskConical };

function Fact({ label, children, title }: { label: string; children: React.ReactNode; title?: string }) {
  return (
    <div className="min-w-0" title={title}>
      <dt className="text-[10.5px] text-subtle">{label}</dt>
      <dd className="truncate text-[12.5px] text-foreground/90">{children}</dd>
    </div>
  );
}

function AccountCard({
  a,
  canSwitch,
  now,
}: {
  a: BrokerAccount;
  canSwitch: boolean;
  now: number;
}) {
  const [confirming, setConfirming] = useState(false);
  const setActive = useSetActiveBroker();
  const h = brokerHealth(a, now);
  const Icon = KIND_ICON[a.kind] ?? FlaskConical;
  const label = safeLabel(a);
  const exp = a.status.token_expires_at;
  // The trader's own 021 account (linked in this panel) can be disconnected here; operator-linked ones cannot.
  const own021 = a.kind === "021" && a.linked_by === "self";
  return (
    <li className={cn("rounded-lg p-3 ring-1", a.active ? "bg-human/[0.05] ring-human/35" : "bg-surface-2/50 ring-hairline")}>
      <div className="flex flex-wrap items-start gap-3">
        <span
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-lg ring-1",
            a.kind === "021" ? "bg-info/12 text-info ring-info/30" : "bg-surface-3 text-muted-foreground ring-hairline-strong",
          )}
        >
          <Icon className="size-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13.5px] font-semibold">
            {brokerName(a.kind)}
            <span className="num text-xs font-normal text-muted-foreground" title={a.kind === "021" ? "Your 021 UCC, masked" : "Paper account id"}>
              {own021 ? maskedOf(a) : displayAccountId(a.account_id, a.kind)}
            </span>
            {own021 && (
              <Chip tone="info" size="xs">
                your account
              </Chip>
            )}
            {a.active && (
              <Chip tone="human" size="xs">
                active
              </Chip>
            )}
          </p>
          <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <Dot tone={h.tone} pulse={h.tone !== "profit"} /> {h.label}
            {label && <span className="truncate text-subtle">· {label}</span>}
          </p>
        </div>
        {canSwitch && !a.active && !confirming && (
          <Button size="sm" variant="outline" onClick={() => setConfirming(true)}>
            <ArrowLeftRight /> Use this account
          </Button>
        )}
      </div>

      <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-3">
        <Fact label="Orders go to">{a.kind === "021" ? "021 simulated exchange" : "Local paper broker"}</Fact>
        <Fact
          label="Access token"
          title={a.kind === "021" ? "Held by the Countersign server. Your browser only sees when it expires." : undefined}
        >
          {a.kind === "021" ? (
            exp ? (
              <span className="num">
                until {dateTime(exp)} IST{now ? ` (${until(exp, now)})` : ""}
              </span>
            ) : (
              <span className="text-subtle">not signed in yet</span>
            )
          ) : (
            <span className="text-subtle">not needed</span>
          )}
        </Fact>
        <Fact label="Market data">{marketDataLabel(a.kind, a.status.market_data)}</Fact>
      </dl>

      {a.status.last_error && (
        <p className="mt-2.5 flex items-start gap-1.5 rounded-md bg-danger/[0.07] px-2.5 py-1.5 text-[11.5px] leading-snug text-danger ring-1 ring-danger/25">
          <CircleAlert className="mt-px size-3.5 shrink-0" aria-hidden />
          <span className="min-w-0 break-words">{a.status.last_error}</span>
        </p>
      )}

      {own021 && !confirming && (
        <div className="mt-3">
          <Disconnect021 account={a} />
        </div>
      )}

      {confirming && (
        <div className="mt-3 space-y-2 rounded-md bg-background/50 p-2.5 ring-1 ring-hairline-strong">
          <p className="text-xs leading-snug text-muted-foreground">
            Switch to <span className="font-medium text-foreground">{brokerName(a.kind)}</span>? Portfolio reads, new drafts and prices
            use it from now on. Orders you already signed stay bound to the account inside their signed hash.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={setActive.isPending}
              onClick={() =>
                setActive.mutate(a.account_id, {
                  onSuccess: () => {
                    setConfirming(false);
                    toast.success(`Now using ${brokerName(a.kind)}`, { description: "Portfolio, orders and prices reload from this account." });
                  },
                  onError: (e) => toast.error("Could not switch accounts", { description: errorMessage(e) }),
                })
              }
            >
              <ArrowLeftRight /> {setActive.isPending ? "Switching..." : "Switch"}
            </Button>
            <Button size="sm" variant="ghost" disabled={setActive.isPending} onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}

/** Paper account from /api/me when the server does not report broker accounts. */
function FallbackCard({ me }: { me: Me }) {
  const { data: sec } = useSecurityStatus();
  const healthy = sec?.broker.healthy;
  return (
    <li className="rounded-lg bg-human/[0.05] p-3 ring-1 ring-human/35">
      <div className="flex items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-surface-3 text-muted-foreground ring-1 ring-hairline-strong">
          <FlaskConical className="size-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13.5px] font-semibold">
            {brokerName(me.account?.kind ?? "paper")}
            <span className="num text-xs font-normal text-muted-foreground">{displayAccountId(accountId(me), me.account?.kind)}</span>
            <Chip tone="human" size="xs">
              active
            </Chip>
          </p>
          <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <Dot tone={healthy === undefined ? "muted" : healthy ? "profit" : "danger"} />
            {healthy === undefined ? "Checking the broker" : healthy ? "Broker reachable" : "Broker unreachable"}
          </p>
        </div>
      </div>
      <p className="mt-2.5 text-[11.5px] text-subtle">This server does not report broker connections yet, so only your account from sign-in is shown.</p>
    </li>
  );
}

/** Security Center panel: which broker this trader's orders go to, its health, and a switch when there are two. */
export function BrokerConnectionPanel({ me }: { me: Me }) {
  const b = useActiveBroker();
  const now = useNow(30_000);
  const ref = useRef<HTMLElement>(null);
  const ready = !b.isPending;

  useEffect(() => {
    if (ready && typeof window !== "undefined" && window.location.hash === "#broker") {
      ref.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }, [ready]);

  const accounts = [...b.accounts].sort((x, y) => Number(y.active) - Number(x.active));
  return (
    <section ref={ref} id="broker" className="scroll-mt-4">
      <Panel
        title="Broker connection"
        icon={Landmark}
        subtitle="Where your orders go and where your prices come from"
        actions={
          b.account ? (
            <Chip tone={b.kind === "021" ? "info" : "muted"} size="xs">
              {b.kind === "021" ? "021 sandbox" : "paper"}
            </Chip>
          ) : undefined
        }
      >
        <div className="space-y-3 p-3">
          {b.isPending ? (
            <SkeletonRows rows={3} className="p-1" />
          ) : b.error ? (
            <ErrorState error={b.error} title="Broker connection status unavailable" compact />
          ) : (
            <ul className="space-y-2">
              {b.reported && accounts.length > 0 ? (
                accounts.map((a) => <AccountCard key={a.account_id} a={a} canSwitch={accounts.length > 1} now={now} />)
              ) : (
                <FallbackCard me={me} />
              )}
            </ul>
          )}
          <Link021Card />
          <div className="flex items-start gap-2 rounded-lg bg-surface-2/50 px-3 py-2.5 text-[11.5px] leading-relaxed text-muted-foreground ring-1 ring-hairline">
            <Info className="mt-0.5 size-3.5 shrink-0 text-info" aria-hidden />
            <p>
              You can connect your own 021 account above, and the operator can also link a 021 account to listed owner emails. New
              accounts trade on the local paper broker, and the Attack Lab always runs on its own paper sandboxes, never on a 021
              account. The 021 access token stays on the server: your browser only sees when it expires. Everything on 021 is virtual
              money on a simulated exchange.
            </p>
          </div>
        </div>
      </Panel>
    </section>
  );
}

/** One line for the account menu: the active broker account, masked, with a health dot. */
export function BrokerMenuLine({ me }: { me: Me }) {
  const b = useActiveBroker();
  const now = useNow(30_000);
  const h = b.account ? brokerHealth(b.account, now) : null;
  const id = b.account?.account_id ?? accountId(me);
  return (
    <span className="mt-1 flex min-w-0 items-center gap-1.5 text-[11px] font-normal text-subtle">
      {h && <Dot tone={h.tone} />}
      <span className="shrink-0">{brokerName(b.kind)}</span>
      <span className="num truncate text-muted-foreground">{displayAccountId(id, b.kind)}</span>
    </span>
  );
}
