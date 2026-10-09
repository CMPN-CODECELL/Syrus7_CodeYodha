"use client";

import { useRef, type CSSProperties, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { useInView } from "./hooks";

/** Fades and lifts its children in the first time they scroll into view. */
export function Reveal({
  children,
  className,
  delay = 0,
}: {
  children: ReactNode;
  className?: string;
  /** Stagger in milliseconds. */
  delay?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const shown = useInView(ref, { once: true, rootMargin: "0px 0px -6% 0px" });
  return (
    <div
      ref={ref}
      data-shown={shown}
      className={cn("lp-reveal", className)}
      style={{ "--lp-delay": `${delay}ms` } as CSSProperties}
    >
      {children}
    </div>
  );
}
