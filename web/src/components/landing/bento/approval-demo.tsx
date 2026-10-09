"use client";

import { Activity, BookOpen, FingerprintPattern, Settings2, User, type LucideIcon } from "lucide-react";
import { useRef, useState } from "react";
import { TONE_CHIP } from "@/components/ui/chip";
import { cn } from "@/lib/utils";
import { useInterval, useLive, useReducedMotion } from "../hooks";

type SourceKey = "you" | "instrument" | "feed" | "default";

const SOURCES: Record<SourceKey, { label: string; icon: LucideIcon; tone: keyof typeof TONE_CHIP; explain: string }> = {
  you: {
    label: "Your instruction",
    icon: User,
    tone: "human",
    explain: "Taken from your own words: “Buy 10 RELIANCE at 2800”. The parser and the model both read it from your message.",
  },
  instrument: {
    label: "Instrument data",
    icon: BookOpen,
    tone: "neutral",
    explain: "From the exchange instrument master: the exact listing, lot size, tick size and freeze limits.",
  },
  feed: {
    label: "Live feed",
    icon: Activity,
    tone: "info",
    explain: "From the market feed at preview time. It anchors your price band; if the price leaves the band, the approval is void.",
  },
  default: {
    label: "System default",
    icon: Settings2,
    tone: "muted",
    explain: "A safe default because you did not say otherwise: delivery (CNC), valid for the day.",
  },
};

const FIELDS: { label: string; value: string; src: SourceKey }[] = [
  { label: "Side", value: "BUY", src: "you" },
  { label: "Quantity", value: "10", src: "you" },
  { label: "Limit price", value: "₹2,800.00", src: "you" },
  { label: "Instrument", value: "NSE:EQ:RELIANCE", src: "instrument" },
  { label: "Reference", value: "₹2,809.20", src: "feed" },
  { label: "Product", value: "CNC, DAY", src: "default" },
];

const WINDOW_S = 60;

/** Countdown ring for the 60 s approval window. */
function CountdownRing({ left }: { left: number }) {
  const r = 17;
  const c = 2 * Math.PI * r;
  const frac = Math.max(0, left) / WINDOW_S;
  const tone = left <= 0 ? "text-danger" : left <= 15 ? "text-warning" : "text-human";
  return (
    <div className={cn("relative size-12 shrink-0", tone)} role="img" aria-label={left > 0 ? `${left} seconds left to sign` : "Approval window closed"}>
      <svg viewBox="0 0 40 40" className="size-12 -rotate-90">
        <circle cx="20" cy="20" r={r} fill="none" stroke="currentColor" strokeOpacity="0.15" strokeWidth="2.5" />
        <circle
          cx="20"
          cy="20"
          r={r}
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - frac)}
          className="transition-[stroke-dashoffset] duration-1000 ease-linear"
        />
      </svg>
      <span className="num absolute inset-0 grid place-items-center text-[13px] font-semibold">{Math.max(0, left)}</span>
    </div>
  );
}

export function ApprovalDemo() {
  const ref = useRef<HTMLDivElement>(null);
  const live = useLive(ref);
  const reduced = useReducedMotion();
  const [left, setLeft] = useState(WINDOW_S);
  const [auto, setAuto] = useState(0);
  const [hover, setHover] = useState<number | null>(null);
  const active = hover ?? auto;

  // The window counts down in real seconds, shows "closed" for a moment, then re-arms.
  useInterval(() => setLeft((l) => (l <= -3 ? WINDOW_S : l - 1)), 1000, live);
  // Without a pointer on it, the card walks through each field's provenance on its own.
  useInterval(() => setAuto((i) => (i + 1) % FIELDS.length), 2400, live && hover === null && !reduced);

  const src = SOURCES[FIELDS[active].src];
  const expired = left <= 0;

  return (
    <div ref={ref} className="relative mx-auto w-full max-w-[560px]" onMouseLeave={() => setHover(null)}>
      <div className="overflow-hidden rounded-xl border border-hairline-strong bg-background/80 shadow-[0_30px_80px_-30px_rgba(0,0,0,0.6)] backdrop-blur">
        <div className={cn("h-[3px] transition-colors duration-500", expired ? "bg-danger/70" : "bg-human")} />
        <div className="flex items-start gap-3 p-4 pb-3">
          <span className="rounded-md bg-human/12 px-2 py-1 text-[12px] font-bold tracking-wide text-human ring-1 ring-human/30">BUY</span>
          <div className="min-w-0 flex-1">
            <p className="flex flex-wrap items-baseline gap-x-2 text-[15px] font-semibold">
              RELIANCE <span className="num text-[11px] font-normal text-subtle">NSE:EQ:RELIANCE</span>
            </p>
            <p className="num mt-1 text-[12.5px] leading-snug text-muted-foreground">BUY 10 RELIANCE @ ₹2,800.00 LIMIT (CNC, DAY)</p>
          </div>
          <CountdownRing left={left} />
        </div>

        <ul className="divide-y divide-hairline border-y border-hairline">
          {FIELDS.map((f, i) => {
            const s = SOURCES[f.src];
            const on = i === active;
            return (
              <li
                key={f.label}
                onMouseEnter={() => setHover(i)}
                className={cn("flex h-10 items-center gap-3 px-4 transition-colors duration-300", on && "bg-foreground/[0.035]")}
              >
                <span className="w-[86px] shrink-0 text-[12px] text-muted-foreground">{f.label}</span>
                <span className="num min-w-0 flex-1 truncate text-[13px] font-semibold">{f.value}</span>
                <button
                  type="button"
                  onFocus={() => setHover(i)}
                  onBlur={() => setHover(null)}
                  aria-describedby="lp-prov-explain"
                  aria-label={`Source of ${f.label}: ${s.label}`}
                  className={cn(
                    "inline-flex h-6 shrink-0 items-center gap-1 overflow-hidden rounded-md px-1.5 text-[11px] font-medium ring-1 ring-inset outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    TONE_CHIP[s.tone],
                  )}
                >
                  <s.icon className="size-3 shrink-0" aria-hidden />
                  <span
                    className={cn(
                      "max-w-0 overflow-hidden whitespace-nowrap opacity-0 transition-[max-width,opacity] duration-300 ease-out",
                      on && "max-w-[8.5rem] opacity-100",
                    )}
                  >
                    {s.label}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>

        <p id="lp-prov-explain" className="min-h-[58px] px-4 pt-3 text-[12.5px] leading-snug text-muted-foreground">
          <span className="font-medium text-foreground">Where did this come from? </span>
          {src.explain}
        </p>

        <div className="flex items-center gap-3 p-4 pt-3">
          <span
            aria-hidden
            className={cn(
              "inline-flex h-10 flex-1 items-center justify-center gap-2 rounded-lg text-[13.5px] font-semibold transition-colors duration-500",
              expired ? "bg-danger/10 text-danger ring-1 ring-danger/30" : "bg-foreground text-background",
            )}
          >
            {expired ? (
              "Window closed. Nothing was sent."
            ) : (
              <>
                <FingerprintPattern className="size-4" />
                Countersign this order
              </>
            )}
          </span>
          <span className="hidden text-[11.5px] leading-tight text-subtle sm:block">
            or app PIN,
            <br />
            or authenticator
          </span>
        </div>
      </div>
    </div>
  );
}
