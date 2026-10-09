// Decorative background for the auth pages: a hairline grid and a twisting ribbon of
// signature strokes. Pure SVG and CSS, rendered on the server, hidden from assistive tech.

const LINES = 22;

const ribbon = Array.from({ length: LINES }, (_, i) => {
  const t = i / (LINES - 1);
  const y0 = 820 + 110 * t;
  const c1y = 1010 - 560 * t;
  const c2y = 90 + 470 * t;
  const y1 = 40 + 170 * t;
  return `M-120 ${y0.toFixed(1)} C 360 ${c1y.toFixed(1)}, 900 ${c2y.toFixed(1)}, 1560 ${y1.toFixed(1)}`;
});

export function AuthBackdrop() {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden>
      <div className="grid-bg absolute inset-0 opacity-60 [background-size:56px_56px] [mask-image:radial-gradient(ellipse_70%_60%_at_50%_40%,black,transparent)]" />
      <div className="absolute -top-48 left-1/2 h-[520px] w-[960px] -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,color-mix(in_oklch,var(--human)_14%,transparent),transparent)]" />
      <svg viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" className="absolute inset-0 size-full opacity-70 dark:opacity-90">
        <defs>
          <linearGradient id="auth-ribbon" x1="0" x2="1" y1="0" y2="0">
            <stop offset="0" stopColor="var(--human)" stopOpacity="0" />
            <stop offset="0.45" stopColor="var(--human)" stopOpacity="0.32" />
            <stop offset="0.8" stopColor="var(--info)" stopOpacity="0.22" />
            <stop offset="1" stopColor="var(--info)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <g fill="none" stroke="url(#auth-ribbon)" strokeWidth="1">
          {ribbon.map((d, i) => (
            <path key={i} d={d} vectorEffect="non-scaling-stroke" />
          ))}
        </g>
        <g fill="none" stroke="var(--foreground)" strokeWidth="1.4" strokeLinecap="round" opacity="0.55">
          {[4, 13, 19].map((i, k) => (
            <path
              key={i}
              d={ribbon[i]}
              pathLength={1000}
              className="auth-spark"
              style={{ animationDelay: `${k * -3.1}s` }}
              vectorEffect="non-scaling-stroke"
            />
          ))}
        </g>
      </svg>
      <div className="absolute inset-x-0 bottom-0 h-40 bg-gradient-to-t from-background to-transparent" />
    </div>
  );
}
