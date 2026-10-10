"use client";

import { useQuery } from "@tanstack/react-query";
import {
  BellRing,
  Check,
  CircleAlert,
  Copy,
  ExternalLink,
  History,
  LoaderCircle,
  MessageCircle,
  MessageCircleOff,
  Pause,
  Play,
  Plus,
  Send,
  ShieldCheck,
  SlidersHorizontal,
  Smartphone,
  SquareTerminal,
  Trash,
  TrendingUp,
  Unlink,
} from "lucide-react";
import { useDeferredValue, useState } from "react";
import { toast } from "sonner";
import { PinSlots } from "@/components/approval/setup-inline";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { Input } from "@/components/ui/input";
import { PageContainer, PageHeader, Panel } from "@/components/ui/panel";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { EmptyState, ErrorState, SkeletonCard } from "@/components/ui/states";
import { Switch } from "@/components/ui/switch";
import { api, IS_MOCK } from "@/lib/api";
import { errorMessage } from "@/lib/errors";
import { ago, time, until } from "@/lib/format";
import { MOCK_WHATSAPP_CODE } from "@/lib/mock";
import { useWhatsApp, useWhatsAppActions, whatsappUnavailable } from "@/lib/queries";
import type { Tone } from "@/lib/status";
import type { WhatsAppInterval, WhatsAppMessage, WhatsAppPrefs, WhatsAppState, WhatsAppWatch } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";

const READ_ONLY_RULE =
  "WhatsApp is a notification and read-only channel. It can never approve, place, modify or cancel a trade. Approvals stay in the app with your passkey, app PIN or authenticator code.";

export function WhatsAppView() {
  const { data, error, isPending, refetch } = useWhatsApp();
  return (
    <PageContainer>
      <PageHeader
        icon={MessageCircle}
        title="WhatsApp alerts"
        description="Fills, standing instruction triggers, security events and price updates on your phone. Ask for your P&L or holdings by message. Nothing on WhatsApp can trade."
      />
      {isPending ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      ) : error ? (
        <ErrorState error={error} title="WhatsApp settings unavailable" onRetry={() => void refetch()} />
      ) : !data ? (
        <Unavailable />
      ) : (
        <Loaded s={data} />
      )}
    </PageContainer>
  );
}

function Unavailable() {
  return (
    <div className="space-y-4">
      <ReadOnlyRule />
      <div className="panel">
        <EmptyState
          icon={MessageCircleOff}
          title="WhatsApp alerts are not available on this server yet"
          description="This Countersign server does not offer WhatsApp alerts. Everything still shows here in the app, and approvals never depend on WhatsApp."
        />
      </div>
    </div>
  );
}

function ReadOnlyRule({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-start gap-2.5 rounded-lg bg-human/[0.06] px-3 py-2.5 ring-1 ring-human/25", className)}>
      <ShieldCheck className="mt-0.5 size-4 shrink-0 text-human" aria-hidden />
      <p className="text-xs leading-relaxed text-muted-foreground">
        <span className="font-medium text-foreground">Read only by design.</span> {READ_ONLY_RULE}
      </p>
    </div>
  );
}

function Loaded({ s }: { s: WhatsAppState }) {
  const live = s.configured && s.mode === "live";
  return (
    <div className="space-y-4">
      <section aria-label="WhatsApp status" className="panel flex flex-col gap-3 p-4 md:flex-row md:items-start">
        <div className="flex min-w-0 items-start gap-3 md:w-[42%]">
          <span
            className={cn(
              "grid size-9 shrink-0 place-items-center rounded-lg ring-1",
              live ? "bg-profit/10 text-profit ring-profit/30" : "bg-warning/10 text-warning ring-warning/30",
            )}
          >
            {live ? <BellRing className="size-4" aria-hidden /> : <History className="size-4" aria-hidden />}
          </span>
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <p className="text-sm font-semibold">{live ? "Delivering on WhatsApp" : "Log only"}</p>
              <Chip tone={live ? "profit" : "warning"} size="xs" pulse={live}>
                {live ? "Live" : "Log only"}
              </Chip>
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">
              {live
                ? "Messages go out through the Twilio WhatsApp sandbox."
                : "Twilio is not configured on this server: messages are recorded below, not sent."}
            </p>
          </div>
        </div>
        <ReadOnlyRule className="md:flex-1" />
      </section>

      <div className="grid items-start gap-4 lg:grid-cols-2">
        <div className="min-w-0 space-y-4">
          <JoinSandbox s={s} />
          <LinkNumber s={s} />
          <Preferences s={s} />
        </div>
        <div className="min-w-0 space-y-4">
          <Watches s={s} />
          <RecentMessages s={s} />
          <Commands />
        </div>
      </div>
    </div>
  );
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      size="sm"
      variant="outline"
      aria-label={`Copy ${label}`}
      onClick={() =>
        void navigator.clipboard
          ?.writeText(value)
          .then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          })
          .catch(() => toast.error("Could not copy", { description: "Select the text and copy it by hand." }))
      }
    >
      {copied ? <Check className="text-profit" /> : <Copy />}
      {copied ? "Copied" : "Copy"}
    </Button>
  );
}

function StepBadge({ n, done }: { n: number; done?: boolean }) {
  return (
    <span
      className={cn(
        "num grid size-5 shrink-0 place-items-center rounded-full text-[11px] font-semibold ring-1",
        done ? "bg-profit/15 text-profit ring-profit/30" : "bg-foreground/[0.06] text-foreground ring-hairline-strong",
      )}
      aria-hidden
    >
      {done ? <Check className="size-3" /> : n}
    </span>
  );
}

function JoinSandbox({ s }: { s: WhatsAppState }) {
  const digits = s.sandbox.number.replace(/\D/g, "");
  const waUrl = digits ? `https://wa.me/${digits}?text=${encodeURIComponent(s.sandbox.join_hint)}` : null;
  return (
    <Panel title={<span className="flex items-center gap-2"><StepBadge n={1} done={!!s.link?.verified} /> Join the Twilio sandbox</span>} icon={Smartphone}>
      <div className="space-y-3 p-4">
        <p className="text-xs text-muted-foreground">From the WhatsApp on your phone, send the join message to the sandbox number once.</p>
        <div className="grid gap-2 sm:grid-cols-2">
          <CopyField label="Sandbox number" value={s.sandbox.number} />
          <CopyField label="Join message" value={s.sandbox.join_hint} mono />
        </div>
        {waUrl && (
          <a
            href={waUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-xs text-info underline-offset-2 hover:underline"
          >
            <ExternalLink className="size-3.5" aria-hidden /> Open WhatsApp with the join message
          </a>
        )}
        <p className="flex items-start gap-2 rounded-lg bg-warning/[0.07] px-3 py-2 text-xs leading-relaxed text-muted-foreground ring-1 ring-warning/25">
          <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
          <span>
            WhatsApp only delivers business messages within 24 hours of your last message to the sandbox. Send{" "}
            <code className="num rounded bg-foreground/[0.07] px-1 text-foreground">HELP</code> before a demo to open the window.
          </span>
        </p>
      </div>
    </Panel>
  );
}

function CopyField({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0 rounded-lg bg-surface-2/50 px-3 py-2 ring-1 ring-hairline">
      <p className="text-[11px] text-muted-foreground">{label}</p>
      <div className="mt-1 flex items-center justify-between gap-2">
        <span className={cn("min-w-0 truncate text-sm font-medium", mono ? "num" : "num tracking-tight")}>{value || "-"}</span>
        {value && <CopyButton value={value} label={label.toLowerCase()} />}
      </div>
    </div>
  );
}

function LinkNumber({ s }: { s: WhatsAppState }) {
  const a = useWhatsAppActions();
  const now = useNow(15_000);
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState<{ phone_masked: string; expires_at: string } | null>(null);
  const [confirmUnlink, setConfirmUnlink] = useState(false);
  const link = s.link;
  const verified = !!link?.verified;
  const awaitingCode = !verified && (sent || link);
  const paused = !!link?.paused_until && new Date(link.paused_until).getTime() > now;

  const sendCode = () => {
    const p = phone.replace(/[\s()-]/g, "");
    a.link.mutate(p.startsWith("+") ? p : `+91${p.replace(/^0/, "")}`, {
      onSuccess: (r) => {
        setSent(r);
        setCode("");
        a.verify.reset();
      },
    });
  };
  const verify = (v: string) =>
    a.verify.mutate(v, {
      onSuccess: () => {
        setSent(null);
        setCode("");
        toast.success("WhatsApp number linked", { description: "Alerts only. Trades are still approved here in the app." });
      },
      onError: () => setCode(""),
    });

  return (
    <Panel
      title={<span className="flex items-center gap-2"><StepBadge n={2} done={verified} /> Link your number</span>}
      icon={MessageCircle}
      actions={verified ? <Chip tone="profit" size="xs" icon={Check}>Verified</Chip> : undefined}
    >
      <div className="space-y-3 p-4">
        {verified && link ? (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-surface-2/50 px-3 py-2.5 ring-1 ring-hairline">
              <div className="min-w-0">
                <p className="num text-sm font-semibold tracking-tight">{link.phone_masked}</p>
                <p className="text-[11px] text-muted-foreground">
                  {!link.opted_in
                    ? "You sent STOP. Send START on WhatsApp to receive alerts again."
                    : paused
                      ? `Paused until ${time(link.paused_until)} (${until(link.paused_until, now)})`
                      : "Receiving alerts"}
                </p>
              </div>
              <Chip tone={!link.opted_in ? "danger" : paused ? "warning" : "profit"} size="xs">
                {!link.opted_in ? "Stopped" : paused ? "Paused" : "On"}
              </Chip>
            </div>
            {link.window_hint && <p className="text-[11px] leading-relaxed text-subtle">{link.window_hint}</p>}
            <div className="flex flex-wrap gap-2">
              {paused ? (
                <Button size="sm" variant="outline" disabled={a.resume.isPending} onClick={() => a.resume.mutate(undefined, { onError: (e) => toast.error("Could not resume", { description: pauseError(e) }) })}>
                  <Play /> Resume
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={a.pause.isPending || !link.opted_in}
                  onClick={() =>
                    a.pause.mutate(60, {
                      onSuccess: () => toast("Alerts paused for 1 hour"),
                      onError: (e) => toast.error("Could not pause", { description: pauseError(e) }),
                    })
                  }
                >
                  <Pause /> Pause 1h
                </Button>
              )}
              <Button
                size="sm"
                variant="outline"
                disabled={a.test.isPending}
                onClick={() =>
                  a.test.mutate(undefined, {
                    onSuccess: () =>
                      toast.success(s.mode === "live" ? "Test message sent" : "Test message logged", {
                        description: s.mode === "live" ? "Check WhatsApp. If it does not arrive, send HELP to the sandbox first." : "Twilio is not configured here, so it is recorded below instead.",
                      }),
                    onError: (e) => toast.error("Test message failed", { description: errorMessage(e) }),
                  })
                }
              >
                {a.test.isPending ? <LoaderCircle className="animate-spin" /> : <Send />} Send test message
              </Button>
              {!confirmUnlink && (
                <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => setConfirmUnlink(true)}>
                  <Unlink /> Unlink
                </Button>
              )}
            </div>
            {confirmUnlink && (
              <div className="space-y-2 rounded-md bg-background/50 p-2.5 ring-1 ring-hairline-strong">
                <p className="text-xs text-muted-foreground">
                  Unlink <span className="num font-medium text-foreground">{link.phone_masked}</span>? Alerts stop and your price watches go quiet until you link a number again.
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    variant="destructive"
                    disabled={a.unlink.isPending}
                    onClick={() =>
                      a.unlink.mutate(undefined, {
                        onSuccess: () => {
                          setConfirmUnlink(false);
                          toast("WhatsApp number unlinked");
                        },
                        onError: (e) => toast.error("Could not unlink", { description: errorMessage(e) }),
                      })
                    }
                  >
                    Unlink number
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setConfirmUnlink(false)}>
                    Keep it
                  </Button>
                </div>
              </div>
            )}
          </>
        ) : (
          <>
            <form
              className="space-y-1.5"
              onSubmit={(e) => {
                e.preventDefault();
                if (phone.trim()) sendCode();
              }}
            >
              <label htmlFor="wa-phone" className="text-xs text-muted-foreground">
                Your WhatsApp number, in international format
              </label>
              <div className="flex gap-2">
                <Input
                  id="wa-phone"
                  type="tel"
                  inputMode="tel"
                  autoComplete="tel"
                  placeholder="+91 98765 43210"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  className="num h-9"
                />
                <Button type="submit" className="h-9" disabled={!phone.trim() || a.link.isPending}>
                  {a.link.isPending ? <LoaderCircle className="animate-spin" /> : <Send />}
                  {awaitingCode ? "Resend" : "Send code"}
                </Button>
              </div>
              <p className="text-[11px] text-subtle">Numbers without a country code are treated as Indian (+91).</p>
            </form>
            {a.link.error && <ErrorState error={a.link.error} title="Code not sent" compact />}
            {awaitingCode && (
              <div className="space-y-2 rounded-lg bg-surface-2/50 p-3 ring-1 ring-hairline">
                <p className="text-xs text-muted-foreground">
                  Enter the 6-digit code we sent on WhatsApp to{" "}
                  <span className="num font-medium text-foreground">{sent?.phone_masked ?? link?.phone_masked}</span>
                  {sent?.expires_at ? `, expires ${until(sent.expires_at, now)}` : ""}. Not there? Join the sandbox first (step 1).
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <PinSlots
                    value={code}
                    onChange={setCode}
                    length={6}
                    align="start"
                    disabled={a.verify.isPending}
                    invalid={!!a.verify.error}
                    label="WhatsApp verification code"
                    onComplete={verify}
                  />
                  <Button size="sm" disabled={code.length !== 6 || a.verify.isPending} onClick={() => verify(code)}>
                    {a.verify.isPending && <LoaderCircle className="animate-spin" />} Verify
                  </Button>
                </div>
                {IS_MOCK && <p className="text-[11px] text-warning">Mock mode code: {MOCK_WHATSAPP_CODE}</p>}
                {a.verify.error && <ErrorState error={a.verify.error} title="Not verified" compact />}
              </div>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}

function pauseError(e: unknown): string {
  if (whatsappUnavailable(e)) return "Pausing from the app is not available on this server. Send PAUSE 1H on WhatsApp instead.";
  return errorMessage(e);
}

const PREFS: { key: keyof WhatsAppPrefs; label: string; hint: string }[] = [
  { key: "orders", label: "Orders", hint: "Placed, filled, rejected or cancelled, for orders you countersigned" },
  { key: "mandates", label: "Standing instructions", hint: "Armed, triggered, filled or rejected" },
  { key: "security", label: "Security", hint: "Kill switch, sign-ins, orders placed outside Countersign" },
  { key: "digests", label: "Daily digests", hint: "Market open and close summary of your P&L and holdings" },
];

function Preferences({ s }: { s: WhatsAppState }) {
  const a = useWhatsAppActions();
  return (
    <Panel title="Alert preferences" icon={SlidersHorizontal} subtitle={s.link?.verified ? undefined : "Saved now, delivered once your number is linked"}>
      <ul className="divide-y divide-hairline">
        {PREFS.map((p) => (
          <li key={p.key} className="flex items-center gap-3 px-4 py-3">
            <div className="min-w-0 flex-1">
              <label htmlFor={`wa-pref-${p.key}`} className="text-sm font-medium">
                {p.label}
              </label>
              <p className="text-xs text-muted-foreground">{p.hint}</p>
            </div>
            <Switch
              id={`wa-pref-${p.key}`}
              checked={s.prefs[p.key]}
              onCheckedChange={(v) =>
                a.prefs.mutate(
                  { ...s.prefs, [p.key]: v },
                  { onError: (e) => toast.error("Preference not saved", { description: errorMessage(e) }) },
                )
              }
            />
          </li>
        ))}
      </ul>
    </Panel>
  );
}

const INTERVALS: WhatsAppInterval[] = [2, 5, 10, 15, 30];

function SymbolSearch({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const q = useDeferredValue(value.trim());
  const res = useQuery({
    queryKey: ["market", "instruments", q],
    queryFn: () => api.instruments(q),
    enabled: open && q.length >= 1,
    staleTime: 60_000,
  });
  // The Attack Lab's look-alike stocks (a Greek letter inside "RELIANCE") are never offered for a watch.
  const hits = (res.data ?? [])
    .filter((i) => i.segment === "EQ" && !(i.flags ?? []).includes("LOOKALIKE_SYMBOL") && /^[\x20-\x7E]+$/.test(i.symbol))
    .slice(0, 6);
  return (
    <div className="relative min-w-0 flex-1">
      <Input
        aria-label="Stock symbol"
        placeholder="Symbol, e.g. TCS"
        value={value}
        autoComplete="off"
        onChange={(e) => {
          onChange(e.target.value.toUpperCase());
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setOpen(false);
        }}
        className="num h-9 uppercase placeholder:normal-case"
      />
      {open && hits.length > 0 && !(hits.length === 1 && hits[0].symbol === value) && (
        <ul role="listbox" aria-label="Matching stocks" className="absolute inset-x-0 top-full z-20 mt-1 overflow-hidden rounded-lg bg-popover py-1 shadow-xl ring-1 ring-hairline-strong">
          {hits.map((i) => (
            <li key={i.instrument_id}>
              <button
                type="button"
                role="option"
                aria-selected={i.symbol === value}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onChange(i.symbol);
                  setOpen(false);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-foreground/[0.06]"
              >
                <span className="num font-medium">{i.symbol}</span>
                <span className="truncate text-xs text-muted-foreground">{i.display_name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function rupees(n: number | null): string {
  if (n === null || !Number.isFinite(n)) return "-";
  return `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function Watches({ s }: { s: WhatsAppState }) {
  const a = useWhatsAppActions();
  const now = useNow(30_000);
  const [symbol, setSymbol] = useState("");
  const [interval, setIntervalMin] = useState<WhatsAppInterval>(5);
  const [minMove, setMinMove] = useState("");
  const [marketHours, setMarketHours] = useState(false);
  const move = minMove.trim() ? Number(minMove) : undefined;
  const moveInvalid = move !== undefined && (!Number.isFinite(move) || move <= 0 || move > 20);

  const add = () =>
    a.addWatch.mutate(
      { symbol: symbol.trim().toUpperCase(), interval_min: interval, ...(move !== undefined ? { min_move_pct: move } : {}), market_hours_only: marketHours },
      {
        onSuccess: () => {
          toast.success(`Watching ${symbol.trim().toUpperCase()}`, { description: `A WhatsApp price update every ${interval} min.` });
          setSymbol("");
          setMinMove("");
        },
      },
    );

  return (
    <Panel title="Price watches" icon={TrendingUp} subtitle="A price update on WhatsApp at your interval; never an order">
      <form
        className="space-y-2.5 border-b border-hairline p-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (symbol.trim() && !moveInvalid) add();
        }}
      >
        <div className="flex flex-wrap gap-2">
          <SymbolSearch value={symbol} onChange={setSymbol} />
          <Select value={String(interval)} onValueChange={(v) => setIntervalMin(Number(v) as WhatsAppInterval)}>
            <SelectTrigger className="h-9 w-36" aria-label="Interval">
              <SelectValue>{(v: string) => `Every ${v} min`}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {INTERVALS.map((i) => (
                <SelectItem key={i} value={String(i)}>
                  Every {i} min
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            Only if moved at least
            <Input
              type="number"
              inputMode="decimal"
              step="0.1"
              min="0.1"
              max="20"
              placeholder="any"
              value={minMove}
              onChange={(e) => setMinMove(e.target.value)}
              aria-invalid={moveInvalid || undefined}
              className="num h-8 w-20"
            />
            %
          </label>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input type="checkbox" checked={marketHours} onChange={(e) => setMarketHours(e.target.checked)} className="size-4 accent-human" />
            Market hours only
          </label>
          <Button type="submit" size="sm" className="ml-auto" disabled={!symbol.trim() || moveInvalid || a.addWatch.isPending}>
            {a.addWatch.isPending ? <LoaderCircle className="animate-spin" /> : <Plus />} Add watch
          </Button>
        </div>
        {moveInvalid && <p className="text-[11px] text-danger">Use a move between 0.1% and 20%, or leave it empty.</p>}
        {a.addWatch.error && <ErrorState error={a.addWatch.error} title="Watch not added" compact />}
      </form>
      {s.watches.length === 0 ? (
        <EmptyState icon={TrendingUp} title="No price watches" description='Add one here, or send "WATCH TCS 5" on WhatsApp.' className="py-8" />
      ) : (
        <ul className="divide-y divide-hairline">
          {s.watches.map((w) => (
            <WatchRow key={w.id} w={w} now={now} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function WatchRow({ w, now }: { w: WhatsAppWatch; now: number }) {
  const a = useWhatsAppActions();
  const busy = a.deleteWatch.isPending || a.toggleWatch.isPending;
  return (
    <li className={cn("flex items-center gap-3 px-4 py-3", !w.active && "opacity-70")}>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="num text-sm font-semibold">{w.symbol}</span>
          <Chip tone="neutral" size="xs">
            every {w.interval_min} min
          </Chip>
          {w.min_move_pct !== null && (
            <Chip tone="info" size="xs">
              moves {w.min_move_pct}%+
            </Chip>
          )}
          {w.market_hours_only && (
            <Chip tone="muted" size="xs">
              market hours
            </Chip>
          )}
        </div>
        <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
          {w.last_sent_at ? (
            <>
              Last sent {ago(w.last_sent_at, now)} at <span className="num text-foreground">{rupees(w.last_price)}</span>
            </>
          ) : (
            "Nothing sent yet"
          )}
        </p>
      </div>
      <Switch
        checked={w.active}
        disabled={busy}
        aria-label={`${w.active ? "Pause" : "Resume"} the ${w.symbol} watch`}
        onCheckedChange={(v) =>
          a.toggleWatch.mutate({ id: w.id, active: v }, { onError: (e) => toast.error("Watch not updated", { description: errorMessage(e) }) })
        }
      />
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={`Delete the ${w.symbol} watch`}
        disabled={busy}
        onClick={() =>
          a.deleteWatch.mutate(w.id, {
            onSuccess: () => toast(`Stopped watching ${w.symbol}`),
            onError: (e) => toast.error("Watch not deleted", { description: errorMessage(e) }),
          })
        }
      >
        <Trash />
      </Button>
    </li>
  );
}

const COMMANDS: { cmd: string; does: string }[] = [
  { cmd: "HELP", does: "List commands; also reopens the 24 h window" },
  { cmd: "PNL", does: "Today's P&L" },
  { cmd: "HOLDINGS", does: "Your holdings" },
  { cmd: "POSITIONS", does: "Open positions" },
  { cmd: "ORDERS", does: "Today's orders" },
  { cmd: "PRICE ITC", does: "Live price of a stock" },
  { cmd: "MANDATES", does: "Armed standing instructions" },
  { cmd: "WATCH TCS 5", does: "Price every 5 min" },
  { cmd: "UNWATCH TCS", does: "Stop a watch" },
  { cmd: "WATCHES", does: "List your watches" },
  { cmd: "PAUSE 2H", does: "Mute alerts for 2 hours" },
  { cmd: "RESUME", does: "Unmute" },
  { cmd: "STOP", does: "Opt out of all messages" },
  { cmd: "START", does: "Opt back in" },
];

function Commands() {
  return (
    <Panel title="Commands" icon={SquareTerminal} subtitle="Send these to the sandbox number">
      <div className="space-y-3 p-4">
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1.5 sm:grid-cols-2">
          {COMMANDS.map((c) => (
            <div key={c.cmd} className="flex min-w-0 items-baseline gap-2">
              <dt>
                <code className="num rounded bg-foreground/[0.07] px-1.5 py-0.5 text-[12px] font-medium whitespace-nowrap text-foreground">{c.cmd}</code>
              </dt>
              <dd className="min-w-0 truncate text-xs text-muted-foreground">{c.does}</dd>
            </div>
          ))}
        </dl>
        <p className="flex items-start gap-2 rounded-lg bg-danger/[0.06] px-3 py-2 text-xs leading-relaxed text-muted-foreground ring-1 ring-danger/25">
          <ShieldCheck className="mt-0.5 size-3.5 shrink-0 text-danger" aria-hidden />
          <span>
            <span className="font-medium text-foreground">BUY and SELL are refused on WhatsApp.</span> So are modify and cancel. Open Countersign and countersign
            with your passkey, app PIN or authenticator code.
          </span>
        </p>
      </div>
    </Panel>
  );
}

const STATUS_TONE: Record<string, Tone> = {
  SENT: "profit",
  DELIVERED: "profit",
  READ: "profit",
  QUEUED: "info",
  LOGGED: "muted",
  PAUSED: "warning",
  SKIPPED: "muted",
  FAILED: "danger",
  UNDELIVERED: "danger",
};

/** Twilio 63016: a free-form message sent more than 24 hours after the trader last wrote to the sandbox. */
function outsideWindow(m: WhatsAppMessage): boolean {
  const st = m.status.toUpperCase();
  if (st.includes("WINDOW")) return true;
  if (st !== "FAILED" && st !== "UNDELIVERED") return false;
  return /63016|24.?h(ou)?r?|window/i.test(m.preview);
}

function RecentMessages({ s }: { s: WhatsAppState }) {
  const now = useNow(30_000);
  return (
    <Panel title="Recent messages" icon={History} subtitle={s.mode === "live" ? "Sent and received on WhatsApp" : "Recorded only: Twilio is not configured here"}>
      {s.recent.length === 0 ? (
        <EmptyState icon={MessageCircle} title="No messages yet" description="Link your number and send a test message, or send HELP to the sandbox." className="py-8" />
      ) : (
        <ul className="divide-y divide-hairline">
          {s.recent.slice(0, 12).map((m, i) => (
            <MessageRow key={`${m.at}-${i}`} m={m} now={now} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function MessageRow({ m, now }: { m: WhatsAppMessage; now: number }) {
  const status = m.status.toUpperCase();
  const windowFail = outsideWindow(m);
  return (
    <li className="space-y-1.5 px-4 py-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Chip tone="neutral" size="xs">
          {m.kind.replace(/_/g, " ").toLowerCase()}
        </Chip>
        <Chip tone={windowFail ? "danger" : STATUS_TONE[status] ?? "neutral"} size="xs">
          {windowFail && !status.includes("FAILED") ? "FAILED" : status}
        </Chip>
        <span className="num ml-auto text-[11px] text-subtle" title={m.at}>
          {ago(m.at, now)}
        </span>
      </div>
      <p className="text-xs leading-relaxed break-words text-muted-foreground">{m.preview}</p>
      {windowFail && <WindowNote />}
    </li>
  );
}

function WindowNote() {
  return (
    <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-warning">
      <CircleAlert className="mt-px size-3 shrink-0" aria-hidden />
      Outside the 24 hour window: WhatsApp blocks business messages when you have not written to the sandbox in the last 24 hours. Send HELP to reopen it;
      new alerts will deliver again.
    </p>
  );
}
