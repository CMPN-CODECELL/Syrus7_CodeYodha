import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import type { Look, Tone } from "@/lib/status";

export const TONE_CHIP: Record<Tone, string> = {
  neutral: "bg-foreground/[0.06] text-foreground/85 ring-hairline-strong",
  muted: "bg-foreground/[0.04] text-muted-foreground ring-hairline",
  profit: "bg-profit/12 text-profit ring-profit/25",
  loss: "bg-loss/12 text-loss ring-loss/25",
  warning: "bg-warning/12 text-warning ring-warning/30",
  danger: "bg-danger/12 text-danger ring-danger/30",
  info: "bg-info/12 text-info ring-info/25",
  ai: "bg-ai/12 text-ai ring-ai/30",
  human: "bg-human/12 text-human ring-human/30",
};

export const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-foreground",
  muted: "text-muted-foreground",
  profit: "text-profit",
  loss: "text-loss",
  warning: "text-warning",
  danger: "text-danger",
  info: "text-info",
  ai: "text-ai",
  human: "text-human",
};

export const TONE_DOT: Record<Tone, string> = {
  neutral: "bg-foreground/60",
  muted: "bg-muted-foreground/60",
  profit: "bg-profit",
  loss: "bg-loss",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
  ai: "bg-ai",
  human: "bg-human",
};

export function Chip({
  tone = "neutral",
  icon: Icon,
  children,
  className,
  pulse,
  size = "sm",
  title,
}: {
  tone?: Tone;
  icon?: LucideIcon;
  children: ReactNode;
  className?: string;
  pulse?: boolean;
  size?: "xs" | "sm" | "md";
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-md font-medium whitespace-nowrap ring-1 ring-inset",
        size === "xs" && "h-[18px] px-1.5 text-[10px] tracking-wide uppercase",
        size === "sm" && "h-5 px-1.5 text-[11px] tracking-wide uppercase",
        size === "md" && "h-6 px-2 text-xs",
        TONE_CHIP[tone],
        className,
      )}
    >
      {pulse ? (
        <span className="relative mr-0.5 flex size-1.5">
          <span className={cn("absolute inline-flex size-full animate-ping rounded-full opacity-60", TONE_DOT[tone])} />
          <span className={cn("relative inline-flex size-1.5 rounded-full", TONE_DOT[tone])} />
        </span>
      ) : Icon ? (
        <Icon className="size-3" aria-hidden />
      ) : null}
      {children}
    </span>
  );
}

export function LookChip({ look, size, className, label }: { look: Look; size?: "xs" | "sm" | "md"; className?: string; label?: string }) {
  return (
    <Chip tone={look.tone} icon={look.icon} pulse={look.pulse} size={size} className={className}>
      {label ?? look.label}
    </Chip>
  );
}

export function SideBadge({ side, size = "md", className }: { side: "BUY" | "SELL"; size?: "sm" | "md" | "lg"; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-md font-semibold tracking-wider",
        size === "sm" && "h-5 px-1.5 text-[10px]",
        size === "md" && "h-6 px-2 text-[11px]",
        size === "lg" && "h-8 px-3 text-sm",
        side === "BUY" ? "bg-profit/15 text-profit ring-1 ring-inset ring-profit/30" : "bg-loss/15 text-loss ring-1 ring-inset ring-loss/30",
        className,
      )}
    >
      {side}
    </span>
  );
}

export function Dot({ tone, pulse, className }: { tone: Tone; pulse?: boolean; className?: string }) {
  return (
    <span className={cn("relative inline-flex size-2 shrink-0", className)}>
      {pulse && <span className={cn("absolute inline-flex size-full animate-ping rounded-full opacity-50", TONE_DOT[tone])} />}
      <span className={cn("relative inline-flex size-2 rounded-full", TONE_DOT[tone])} />
    </span>
  );
}
