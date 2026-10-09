"use client";

import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";

/** Monospace hash/id with copy-to-clipboard. `wrap` shows the whole value on several lines instead of truncating it. */
export function Hash({
  value,
  display,
  className,
  copy = true,
  label,
  wrap = false,
}: {
  value: string;
  display?: string;
  className?: string;
  copy?: boolean;
  label?: string;
  wrap?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <span className={cn("group/hash inline-flex max-w-full items-center gap-1 font-mono text-[12px] tabular-nums", className)}>
      <span className={wrap ? "min-w-0 break-words" : "truncate"} title={value}>
        {display ?? value}
      </span>
      {copy && (
        <button
          type="button"
          aria-label={label ? `Copy ${label}` : "Copy"}
          className="grid size-5 shrink-0 place-items-center rounded text-muted-foreground opacity-60 transition hover:bg-foreground/10 hover:text-foreground hover:opacity-100 focus-visible:opacity-100"
          onClick={() => {
            void navigator.clipboard?.writeText(value).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1200);
            });
          }}
        >
          {copied ? <Check className="size-3 text-profit" /> : <Copy className="size-3" />}
        </button>
      )}
    </span>
  );
}
