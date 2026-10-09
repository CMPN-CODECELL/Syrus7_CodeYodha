"use client";

import { FileCode2, FileText } from "lucide-react";
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { useInterval, useLive, useReducedMotion } from "../hooks";

// An example ApprovalPayload (the real schema; illustrative values). PAYLOAD_HASH is the actual
// SHA-256 of this payload's RFC 8785 canonical form, including the fields shortened below.
const PAYLOAD_HASH = "57578100c096edfb4be6d0baf800cc3c842794d37b061b3add230cae1d35c526";

const k = (s: string) => <span className="text-info">&quot;{s}&quot;</span>;
const str = (s: string) => <span className="text-human">&quot;{s}&quot;</span>;
const num = (n: number | string) => <span className="text-warning">{n}</span>;
const p = (s: string) => <span className="text-subtle">{s}</span>;

const LINES: ReactNode[] = [
  p("{"),
  <>  {k("version")}{p(": ")}{num(1)}{p(",")}</>,
  <>  {k("purpose")}{p(": ")}{str("ORDER")}{p(",")}</>,
  <>  {k("proposal_id")}{p(": ")}{str("3f6b2c1e-8d4a-4f0b-…")}{p(",")}</>,
  <>  {k("canonical_hash")}{p(": ")}{str("07833badff78…9099d814")}{p(",")}</>,
  <>  {k("account_id")}{p(": ")}{str("PTQ7M2XK")}{p(",")}</>,
  <>  {k("price_refs")}{p(": [{")}</>,
  <>    {k("instrument_id")}{p(": ")}{str("NSE:EQ:RELIANCE")}{p(",")}</>,
  <>    {k("reference_price_paise")}{p(": ")}{num(280920)}{p(",")}</>,
  <>    {k("band_low_paise")}{p(": ")}{num(278111)}{p(",")}</>,
  <>    {k("band_high_paise")}{p(": ")}{num(283729)}</>,
  <>  {p("}],")}</>,
  <>  {k("nonce")}{p(": ")}{str("c2f0b1a47e9d4c55…")}{p(",")}</>,
  <>  {k("issued_at")}{p(": ")}{str("2026-10-09T04:45:02Z")}{p(",")}</>,
  <>  {k("expires_at")}{p(": ")}{str("2026-10-09T04:46:02Z")}{p(",")}</>,
  <>  <span className="text-subtle italic">{"/* user_id, credential_ref, mandate_id */"}</span></>,
  p("}"),
];

const TABS = [
  { id: "payload", label: "payload.json", icon: FileCode2 },
  { id: "hash", label: "hash.txt", icon: FileText },
] as const;
type TabId = (typeof TABS)[number]["id"];

export function PayloadWindow() {
  const ref = useRef<HTMLDivElement>(null);
  const live = useLive(ref);
  const reduced = useReducedMotion();
  const [tab, setTab] = useState<TabId>("payload");
  const [touched, setTouched] = useState(false);
  const [typed, setTyped] = useState(0);
  const [elapsed, setElapsed] = useState(0);

  // Until the visitor picks a tab, alternate between the payload and its hash.
  useInterval(
    () => {
      setElapsed((e) => e + 1);
      if ((elapsed + 1) % 8 === 0) {
        setTab((t) => (t === "payload" ? "hash" : "payload"));
        setTyped(0);
      }
    },
    1000,
    live && !touched && !reduced,
  );
  // Type the hash out, one hex digit at a time.
  useInterval(() => setTyped((n) => Math.min(PAYLOAD_HASH.length, n + 1)), 28, live && tab === "hash" && typed < PAYLOAD_HASH.length && !reduced);

  const select = (id: TabId) => {
    setTouched(true);
    if (id !== tab) setTyped(0);
    setTab(id);
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const next = tab === "payload" ? "hash" : "payload";
      select(next);
      document.getElementById(`lp-code-tab-${next}`)?.focus();
    }
  };
  const shown = reduced ? PAYLOAD_HASH.length : typed;

  return (
    <div ref={ref} className="flex flex-1 flex-col overflow-hidden rounded-xl border border-hairline-strong bg-background/85 shadow-[0_30px_80px_-30px_rgba(0,0,0,0.55)]">
      <div className="flex items-center gap-3 border-b border-hairline px-3 sm:px-4">
        <div className="flex gap-1.5" aria-hidden>
          <span className="size-2.5 rounded-full bg-foreground/15" />
          <span className="size-2.5 rounded-full bg-foreground/15" />
          <span className="size-2.5 rounded-full bg-foreground/15" />
        </div>
        <div role="tablist" aria-label="Signed payload files" className="flex min-w-0" onKeyDown={onKey}>
          {TABS.map((t) => {
            const on = tab === t.id;
            return (
              <button
                key={t.id}
                id={`lp-code-tab-${t.id}`}
                type="button"
                role="tab"
                aria-selected={on}
                aria-controls="lp-code-panel"
                tabIndex={on ? 0 : -1}
                onClick={() => select(t.id)}
                className={cn(
                  "relative flex h-11 items-center gap-1.5 px-3 text-[12.5px] transition-colors outline-none focus-visible:bg-foreground/[0.06]",
                  on ? "text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                <t.icon className={cn("size-3.5", on && "text-human")} aria-hidden />
                {t.label}
                {on && <span className="absolute inset-x-2 bottom-0 h-px bg-human" aria-hidden />}
              </button>
            );
          })}
        </div>
        <span className="ml-auto hidden rounded border border-hairline px-1.5 py-0.5 text-[10px] tracking-wide text-subtle uppercase sm:inline">Example</span>
      </div>

      <div id="lp-code-panel" role="tabpanel" aria-labelledby={`lp-code-tab-${tab}`} className="min-h-[362px] flex-1 basis-0 overflow-auto p-4 font-mono text-[11.5px] leading-[1.75] sm:text-[12.5px]">
        {tab === "payload" ? (
          <pre className="m-0 whitespace-pre">
            {LINES.map((l, i) => (
              <div key={i} className="flex">
                <span className="mr-4 inline-block w-5 shrink-0 text-right text-subtle/60 select-none" aria-hidden>
                  {i + 1}
                </span>
                <span>{l}</span>
              </div>
            ))}
          </pre>
        ) : (
          <div className="space-y-4">
            <p className="text-subtle"># The challenge your device signs</p>
            <p>
              <span className="text-human">$</span> jcs payload.json <span className="text-subtle">|</span> sha256sum
            </p>
            <p className="rounded-lg bg-human/[0.07] p-3 text-[13px] leading-relaxed tracking-wide break-all text-foreground ring-1 ring-human/20 sm:text-[14px]">
              <span aria-hidden>{PAYLOAD_HASH.slice(0, shown)}</span>
              {shown < PAYLOAD_HASH.length && <span className="lp-caret ml-px inline-block h-[1.1em] w-[0.55em] translate-y-[0.15em] bg-human" aria-hidden />}
              <span className="sr-only">{PAYLOAD_HASH}</span>
            </p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[12px]">
              <dt className="text-subtle">canonical form</dt>
              <dd>RFC 8785 JSON (sorted keys, no whitespace)</dd>
              <dt className="text-subtle">signed by</dt>
              <dd>your passkey, over exactly this hash</dd>
              <dt className="text-subtle">valid until</dt>
              <dd>issued_at + 60 s, single use</dd>
              <dt className="text-subtle">re-checked by</dt>
              <dd>the executor, before anything is sent</dd>
            </dl>
          </div>
        )}
      </div>
    </div>
  );
}
