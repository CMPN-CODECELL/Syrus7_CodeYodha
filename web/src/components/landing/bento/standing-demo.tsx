"use client";

import { Check, CircleSlash } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { useLive, useReducedMotion } from "../hooks";
import { rand } from "../scenes/util";

// "Buy 10 ABCCORP if it falls to ₹500.00": the price dips through the trigger twice; the
// standing instruction fires on the first crossing only.

const TRIGGER = 500;
const ANCHORS: [number, number][] = [
  [0, 505.8], [0.12, 504.6], [0.22, 503.9], [0.32, 501.8], [0.38, 500.8], [0.45, 499.2], [0.52, 499.6],
  [0.6, 502.2], [0.68, 503.4], [0.76, 501.6], [0.83, 499.6], [0.9, 499.3], [1, 500.4],
];
const N = 72;
const Y_MIN = 498.2;
const Y_MAX = 506.8;

function sample(x: number): number {
  let i = 1;
  while (i < ANCHORS.length - 1 && ANCHORS[i][0] < x) i++;
  const [x0, y0] = ANCHORS[i - 1];
  const [x1, y1] = ANCHORS[i];
  const k = (x - x0) / (x1 - x0);
  return y0 + (y1 - y0) * (k * k * (3 - 2 * k));
}

const SERIES = Array.from({ length: N }, (_, i) => {
  const x = i / (N - 1);
  const base = sample(x);
  // Tick noise, damped near the trigger so the line never chatters across it.
  const damp = Math.min(1, Math.abs(base - TRIGGER) / 0.7);
  return [x, base + (rand(i + 21) - 0.5) * 0.55 * damp] as const;
});

// Downward crossings of the trigger, as x fractions (linear interpolation between ticks).
const CROSSINGS = SERIES.flatMap(([x, y], i) => {
  if (i === 0) return [];
  const [px, py] = SERIES[i - 1];
  if (py > TRIGGER && y <= TRIGGER) return [px + ((py - TRIGGER) / (py - y)) * (x - px)];
  return [];
});

const W = 600;
const H = 220;
const sx = (x: number) => x * W;
const sy = (v: number) => H - ((v - Y_MIN) / (Y_MAX - Y_MIN)) * H;
const LINE = SERIES.map(([x, y], i) => `${i ? "L" : "M"}${sx(x).toFixed(1)} ${sy(y).toFixed(1)}`).join(" ");
const AREA = `${LINE} L${W} ${H} L0 ${H} Z`;
const pct = (v: number, max: number) => `${(v / max) * 100}%`;
const priceAt = (x: number) => SERIES[Math.min(N - 1, Math.round(x * (N - 1)))][1];

const DRAW_S = 6.5;
const CYCLE_S = 9;

export function StandingDemo() {
  const ref = useRef<HTMLDivElement>(null);
  const live = useLive(ref);
  const reduced = useReducedMotion();
  const [progress, setProgress] = useState(0.62);

  useEffect(() => {
    if (!live || reduced) return;
    let raf = 0;
    let start = 0;
    let lastShown = -1;
    const loop = (now: number) => {
      // Begin a little way in, so the chart is never empty when it scrolls into view.
      if (!start) start = now - 1600;
      const t = ((now - start) / 1000) % CYCLE_S;
      const p = Math.min(1, t / DRAW_S);
      if (Math.abs(p - lastShown) > 0.003 || (p === 1 && lastShown !== 1)) {
        lastShown = p;
        setProgress(p);
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [live, reduced]);

  const p = reduced ? 1 : progress;
  const [first, second] = CROSSINGS;
  const fired = first !== undefined && p >= first;
  const recrossed = second !== undefined && p >= second;
  const headY = priceAt(p);

  return (
    <div ref={ref} className="relative">
      <div className="mb-4 flex flex-wrap items-center gap-2 text-[12px]">
        <span className="rounded-md bg-foreground/[0.06] px-2 py-1 font-medium ring-1 ring-hairline-strong">BUY 10 ABCCORP</span>
        <span className="text-muted-foreground">when LTP ≤</span>
        <span className="num rounded-md bg-human/10 px-2 py-1 font-semibold text-human ring-1 ring-human/25">₹500.00</span>
        <span
          className={cn(
            "ml-auto inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold tracking-wide uppercase ring-1 transition-colors duration-500",
            fired ? "bg-human/12 text-human ring-human/30" : "bg-foreground/[0.05] text-muted-foreground ring-hairline-strong",
          )}
        >
          <span className={cn("size-1.5 rounded-full", fired ? "bg-human" : "bg-muted-foreground")} aria-hidden />
          {fired ? "Fired, used up" : "Armed"}
        </span>
      </div>

      <div
        className="relative h-[220px]"
        role="img"
        aria-label="ABCCORP price chart. The price falls through the ₹500 trigger, the standing instruction fires once, and a later crossing does not fire it again."
      >
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="absolute inset-0 size-full overflow-visible" aria-hidden>
          <defs>
            <linearGradient id="lp-standing-area" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0" stopColor="var(--human)" stopOpacity="0.22" />
              <stop offset="1" stopColor="var(--human)" stopOpacity="0" />
            </linearGradient>
            <clipPath id="lp-standing-clip">
              <rect x="0" y="-20" width={W * p} height={H + 40} />
            </clipPath>
          </defs>
          {[499, 501, 503, 505].map((v) => (
            <line key={v} x1="0" x2={W} y1={sy(v)} y2={sy(v)} stroke="var(--hairline)" vectorEffect="non-scaling-stroke" />
          ))}
          <g clipPath="url(#lp-standing-clip)">
            <path d={AREA} fill="url(#lp-standing-area)" />
            <path d={LINE} fill="none" stroke="var(--foreground)" strokeOpacity="0.85" strokeWidth="1.6" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
          </g>
          <line
            x1="0"
            x2={W}
            y1={sy(TRIGGER)}
            y2={sy(TRIGGER)}
            stroke="var(--human)"
            strokeWidth="1.2"
            strokeDasharray="5 5"
            vectorEffect="non-scaling-stroke"
          />
        </svg>

        <span className="num absolute left-0 translate-y-[30%] rounded bg-background/80 px-1 text-[10.5px] text-human" style={{ top: pct(sy(TRIGGER), H) }}>
          trigger 500.00
        </span>
        {[501, 503, 505].map((v) => (
          <span key={v} className="num absolute right-0 -translate-y-[120%] text-[10px] text-subtle" style={{ top: pct(sy(v), H) }} aria-hidden>
            {v}.00
          </span>
        ))}

        {/* Live head of the line. */}
        {p < 1 && (
          <span
            className="absolute size-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-foreground ring-4 ring-foreground/15"
            style={{ left: pct(sx(p), W), top: pct(sy(headY), H) }}
            aria-hidden
          />
        )}

        {first !== undefined && (
          <div
            className={cn("absolute transition-[opacity,transform] duration-500", fired ? "opacity-100" : "translate-y-1 opacity-0")}
            style={{ left: pct(sx(first), W), top: pct(sy(TRIGGER), H) }}
            aria-hidden
          >
            <span className="absolute size-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-human ring-4 ring-human/25" />
            <span className="absolute bottom-3 left-0 -translate-x-1/2 rounded-md bg-human px-2 py-1 text-[11px] font-semibold whitespace-nowrap text-background shadow-lg">
              <Check className="mr-1 inline size-3" strokeWidth={3} />
              Fired once
            </span>
          </div>
        )}
        {second !== undefined && (
          <div
            className={cn("absolute transition-[opacity,transform] duration-500", recrossed ? "opacity-100" : "translate-y-1 opacity-0")}
            style={{ left: pct(sx(second), W), top: pct(sy(TRIGGER), H) }}
            aria-hidden
          >
            <span className="absolute size-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-muted-foreground bg-background" />
            <span className="absolute top-3 right-0 translate-x-1/3 rounded-md border border-hairline-strong bg-background/90 px-2 py-1 text-[11px] font-medium whitespace-nowrap text-muted-foreground shadow-lg">
              <CircleSlash className="mr-1 inline size-3" />
              Crossed again, not fired
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
