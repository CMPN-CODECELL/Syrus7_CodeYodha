import { cn } from "@/lib/utils";

/** Countersign mark: the AI's dot (violet) above the human's signature stroke (emerald). */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 28 28" className={cn("size-7", className)} aria-hidden>
      <rect x="0.5" y="0.5" width="27" height="27" rx="8" className="fill-surface-2 stroke-hairline-strong" />
      <path
        d="M6 18.2c2.4 0 3.1-7.2 5.4-7.2 2.2 0 1.3 6.6 3.5 6.6 2.1 0 2.4-3.8 6.6-3.8"
        fill="none"
        stroke="var(--human)"
        strokeWidth="2.1"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M6.5 21.6h15" stroke="var(--human)" strokeOpacity="0.35" strokeWidth="1.2" strokeLinecap="round" />
      <circle cx="20.6" cy="7.6" r="2.1" fill="var(--ai)" />
    </svg>
  );
}

export function BrandWordmark({ className }: { className?: string }) {
  return (
    <span className={cn("flex items-center gap-2", className)}>
      <BrandMark />
      <span className="text-[15px] font-semibold tracking-tight">Countersign</span>
    </span>
  );
}
