"use client";

import { useEffect, useRef, useState } from "react";
import { count as fmt } from "./format";
import { useInView, useReducedMotion } from "./hooks";

/**
 * Counts up to `to` the first time it scrolls into view. Server-rendered with the final value,
 * so it is correct without JavaScript and under reduced motion.
 */
export function CountUp({ to, duration = 1400 }: { to: number; duration?: number }) {
  const ref = useRef<HTMLSpanElement>(null);
  const seen = useInView(ref, { once: true, rootMargin: "0px 0px -10% 0px" });
  const reduced = useReducedMotion();
  const [value, setValue] = useState(to);

  useEffect(() => {
    if (!seen || reduced) return;
    let raf = 0;
    let start = 0;
    const loop = (now: number) => {
      if (!start) start = now;
      const k = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - k, 4);
      setValue(Math.round(to * eased));
      if (k < 1) raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [seen, reduced, to, duration]);

  return (
    <span ref={ref} className="tabular-nums">
      <span aria-hidden>{fmt(value)}</span>
      <span className="sr-only">{fmt(to)}</span>
    </span>
  );
}
