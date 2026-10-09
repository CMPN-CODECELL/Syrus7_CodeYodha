"use client";

import { CircleCheck, RotateCcw, TriangleAlert } from "lucide-react";
import { useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { inr, price } from "../format";
import { useInterval, useLive, useReducedMotion } from "../hooks";

// RELIANCE reference ₹2,809.20 with the default 100 bps band: ₹2,781.11 to ₹2,837.29.
const REF = 280920;
const LO = 278111;
const HI = 283729;
const PAD = (HI - LO) * 0.42;
const MIN = LO - PAD;
const MAX = HI + PAD;

// A scripted wander (paise from the reference): drifts, then breaks out of the band, first up, then down.
const UP = [0, 240, -180, 420, 160, -390, -620, -210, 350, 780, 1240, 1690, 2210, 2650, 3180, 3420];
const PATH = [...UP, ...UP.map((v) => -v)];
const BREAK_HOLD = 4;
const REARM_HOLD = 3;
const TRAIL = 22;

type Phase = "armed" | "void" | "rearmed";
interface Sim {
  step: number;
  phase: Phase;
  hold: number;
  trail: number[];
}

// A quiet prelude so the chart starts full rather than empty.
const PRELUDE = [-120, 80, 210, -60, -240, -90, 130, 300, 170, -40, -280, -150, 60, 140, -30];
const START = 6;
const INITIAL: Sim = {
  step: START,
  phase: "armed",
  hold: 0,
  trail: [...PRELUDE, ...PATH.slice(0, START + 1)].map((v) => REF + v).slice(-TRAIL),
};

function advance(s: Sim): Sim {
  if (s.phase === "void") {
    if (s.hold < BREAK_HOLD) return { ...s, hold: s.hold + 1 };
    // Re-arm: a fresh preview at the reference price; the trader must sign again.
    return { step: s.step < UP.length ? UP.length : 0, phase: "rearmed", hold: 0, trail: [...s.trail, REF].slice(-TRAIL) };
  }
  if (s.phase === "rearmed" && s.hold < REARM_HOLD) return { ...s, hold: s.hold + 1 };
  const step = (s.step + 1) % PATH.length;
  const ltp = REF + PATH[step];
  return { step, phase: ltp < LO || ltp > HI ? "void" : "armed", hold: 0, trail: [...s.trail, ltp].slice(-TRAIL) };
}

const pos = (v: number) => ((Math.min(MAX, Math.max(MIN, v)) - MIN) / (MAX - MIN)) * 100;
const pct = (v: number) => `${pos(v)}%`;

/** Recent ticks against the signed band; ticks outside it are drawn in red. */
function Trail({ trail }: { trail: number[] }) {
  const w = 300;
  const h = 96;
  const y = (v: number) => h - (pos(v) / 100) * h;
  const x = (i: number) => (i / (TRAIL - 1)) * w;
  const offset = TRAIL - trail.length;
  const d = trail.map((v, i) => `${i ? "L" : "M"}${x(i + offset).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const last = trail[trail.length - 1];
  const out = (v: number) => v < LO || v > HI;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="h-full min-h-24 w-full overflow-visible" aria-hidden>
      <rect x="0" y={y(HI)} width={w} height={y(LO) - y(HI)} fill="var(--human)" fillOpacity="0.08" />
      <line x1="0" x2={w} y1={y(HI)} y2={y(HI)} stroke="var(--human)" strokeOpacity="0.5" strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />
      <line x1="0" x2={w} y1={y(LO)} y2={y(LO)} stroke="var(--human)" strokeOpacity="0.5" strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />
      <line x1="0" x2={w} y1={y(REF)} y2={y(REF)} stroke="var(--foreground)" strokeOpacity="0.15" vectorEffect="non-scaling-stroke" />
      <path d={d} fill="none" stroke="var(--foreground)" strokeOpacity="0.8" strokeWidth="1.5" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      {trail.map((v, i) =>
        out(v) ? <circle key={i} cx={x(i + offset)} cy={y(v)} r="2.6" fill="var(--danger)" /> : null,
      )}
      {last !== undefined && (
        <circle cx={x(TRAIL - 1)} cy={y(last)} r="3.5" fill={out(last) ? "var(--danger)" : "var(--foreground)"} />
      )}
    </svg>
  );
}

export function BandDemo() {
  const ref = useRef<HTMLDivElement>(null);
  const live = useLive(ref);
  const reduced = useReducedMotion();
  const [sim, setSim] = useState<Sim>(INITIAL);
  useInterval(() => setSim(advance), 650, live && !reduced);

  const { phase } = sim;
  const ltp = phase === "rearmed" ? REF : REF + PATH[sim.step];
  const out = ltp < LO || ltp > HI;

  return (
    <div ref={ref} className="flex flex-1 flex-col gap-4">
      <div className="flex items-center justify-between text-[11px] font-semibold tracking-[0.12em] text-muted-foreground uppercase">
        <span>Approved band · RELIANCE</span>
        <span className={cn("num tracking-normal normal-case transition-colors", out ? "text-danger" : "text-foreground")}>LTP {inr(ltp)}</span>
      </div>

      <div className="flex min-h-28 flex-1 rounded-lg border border-hairline bg-background/40 px-3 py-3">
        <Trail trail={sim.trail} />
      </div>

      <div
        className="relative h-14 select-none"
        role="img"
        aria-label={`Approved band ${inr(LO)} to ${inr(HI)}. Live price ${inr(ltp)}${out ? ", outside the band: the approval is void" : ", inside the band"}.`}
      >
        <div className="absolute inset-x-0 top-[26px] h-1 rounded-full bg-foreground/[0.07]" />
        <div className="absolute inset-x-0 top-[38px] flex justify-between px-px" aria-hidden>
          {Array.from({ length: 25 }, (_, i) => (
            <span key={i} className={cn("w-px bg-foreground/15", i % 6 === 0 ? "h-2" : "h-1")} />
          ))}
        </div>
        <div
          className={cn(
            "absolute top-[18px] h-5 rounded-md ring-1 transition-colors duration-500 ring-inset",
            out ? "bg-danger/12 ring-danger/45" : "bg-human/15 ring-human/45",
          )}
          style={{ left: pct(LO), width: `calc(${pct(HI)} - ${pct(LO)})` }}
        />
        <div className="absolute top-[12px] h-8 w-px border-l border-dashed border-foreground/35" style={{ left: pct(REF) }} />
        <div className="absolute top-0 -translate-x-1/2 transition-[left] duration-500 ease-out" style={{ left: pct(ltp) }}>
          <div className={cn("mx-auto h-14 w-[3px] rounded-full transition-colors duration-300", out ? "bg-danger" : "bg-foreground")} />
          <div className={cn("absolute top-[22px] left-1/2 size-3 -translate-x-1/2 rounded-full ring-4 transition-colors", out ? "bg-danger ring-danger/25" : "bg-foreground ring-foreground/15")} />
        </div>
      </div>

      <div className="relative h-4 text-[11px]">
        <span className="num absolute -translate-x-1/2 text-muted-foreground" style={{ left: pct(LO) }}>
          {price(LO)}
        </span>
        <span className="num absolute -translate-x-1/2 text-subtle" style={{ left: pct(REF) }}>
          ref
        </span>
        <span className="num absolute -translate-x-1/2 text-muted-foreground" style={{ left: pct(HI) }}>
          {price(HI)}
        </span>
      </div>

      <div
        className={cn(
          "flex min-h-[52px] items-start gap-2.5 rounded-lg px-3 py-2.5 text-[12.5px] leading-snug ring-1 transition-colors duration-500",
          phase === "void"
            ? "bg-danger/10 text-danger ring-danger/30"
            : phase === "rearmed"
              ? "bg-info/10 text-info ring-info/25"
              : "bg-human/[0.08] text-human ring-human/25",
        )}
      >
        {phase === "void" ? (
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
        ) : phase === "rearmed" ? (
          <RotateCcw className="mt-0.5 size-4 shrink-0" aria-hidden />
        ) : (
          <CircleCheck className="mt-0.5 size-4 shrink-0" aria-hidden />
        )}
        <span>
          {phase === "void"
            ? "Price left your band. This approval is void and nothing is sent."
            : phase === "rearmed"
              ? "Fresh preview at the new price. You review and sign again."
              : "Inside the band you saw. Your signature is still valid."}
        </span>
      </div>
    </div>
  );
}
