// Shared class names for landing buttons and labels (server-safe, no hooks).

export const btnPrimary =
  "group/btn inline-flex h-12 items-center justify-center gap-2 rounded-lg bg-foreground px-5 text-[15px] font-medium text-background shadow-[0_10px_40px_-14px_color-mix(in_oklch,var(--human)_70%,transparent)] transition-[background-color,transform,box-shadow] duration-200 hover:bg-foreground/90 hover:shadow-[0_14px_50px_-12px_color-mix(in_oklch,var(--human)_80%,transparent)] active:translate-y-px focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

export const btnSecondary =
  "group/btn inline-flex h-12 items-center justify-center gap-2 rounded-lg border border-hairline-strong bg-surface-1/50 px-5 text-[15px] font-medium text-foreground backdrop-blur-md transition-colors duration-200 hover:bg-surface-2/80 active:translate-y-px focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

export const arrowNudge = "size-4 transition-transform duration-200 group-hover/btn:translate-x-0.5";

export const eyebrow = "font-mono text-[11px] font-medium tracking-[0.2em] text-subtle uppercase";

export const container = "mx-auto w-full max-w-[1240px] px-4 sm:px-6 lg:px-8";
