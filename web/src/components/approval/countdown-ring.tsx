"use client";

import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

/** The approval window as a draining ring. 60 s from approval/start; colour shifts as it closes. */
export function CountdownRing({
  issuedAt,
  expiresAt,
  size = 64,
  className,
}: {
  issuedAt: string;
  expiresAt: string;
  size?: number;
  className?: string;
}) {
  const now = useNow(250);
  const start = Date.parse(issuedAt);
  const end = Date.parse(expiresAt);
  const total = Math.max(1, end - start);
  const remaining = now ? Math.max(0, end - now) : total;
  const frac = remaining / total;
  const secs = Math.ceil(remaining / 1000);
  const r = (size - 8) / 2;
  const c = 2 * Math.PI * r;
  const color = remaining === 0 ? "var(--danger)" : secs <= 5 ? "var(--danger)" : secs <= 15 ? "var(--warning)" : "var(--human)";
  return (
    <div
      className={cn("relative shrink-0", className)}
      style={{ width: size, height: size }}
      role="timer"
      aria-live="off"
      aria-label={remaining === 0 ? "Approval window expired" : `${secs} seconds left in the approval window`}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--hairline-strong)" strokeWidth={4} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={4}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - frac)}
          style={{ transition: "stroke-dashoffset 250ms linear, stroke 300ms" }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center leading-none">
        <span className="num text-[15px] font-semibold" style={{ color }}>
          {remaining === 0 ? "0" : secs}
        </span>
        <span className="mt-0.5 text-[9px] tracking-wider text-subtle uppercase">{remaining === 0 ? "expired" : "sec"}</span>
      </div>
    </div>
  );
}

export function useWindowExpired(expiresAt: string | undefined) {
  const now = useNow(500);
  if (!expiresAt || !now) return false;
  return now >= Date.parse(expiresAt);
}
