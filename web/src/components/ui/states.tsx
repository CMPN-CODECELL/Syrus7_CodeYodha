"use client";

import { CircleAlert, RotateCcw, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage, isApiError } from "@/lib/errors";
import { cn } from "@/lib/utils";

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 px-6 py-10 text-center", className)}>
      <div className="relative grid size-12 place-items-center rounded-xl bg-surface-2 ring-1 ring-hairline">
        <Icon className="size-5 text-muted-foreground" aria-hidden />
        <div className="grid-bg absolute inset-0 -z-10 scale-[2.2] rounded-full opacity-40 [mask-image:radial-gradient(closest-side,black,transparent)]" />
      </div>
      <div className="space-y-1">
        <p className="text-sm font-medium">{title}</p>
        {description && <p className="mx-auto max-w-sm text-xs leading-relaxed text-muted-foreground">{description}</p>}
      </div>
      {action}
    </div>
  );
}

export function ErrorState({
  error,
  title = "Could not load this data",
  onRetry,
  className,
  compact,
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
  compact?: boolean;
}) {
  const code = isApiError(error) ? error.code : null;
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-3 rounded-lg border border-danger/25 bg-danger/[0.06] p-3 text-left",
        compact ? "text-xs" : "text-sm",
        className,
      )}
    >
      <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="font-medium text-foreground">{title}</p>
        <p className="text-muted-foreground">{errorMessage(error)}</p>
        {code && <p className="num text-[11px] text-subtle">{code}</p>}
      </div>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry} className="shrink-0">
          <RotateCcw /> Retry
        </Button>
      )}
    </div>
  );
}

export function SkeletonRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-2 p-4", className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="h-4 w-16" />
        </div>
      ))}
    </div>
  );
}

export function SkeletonCard({ className, lines = 4 }: { className?: string; lines?: number }) {
  return (
    <div className={cn("panel space-y-3 p-4", className)} aria-busy="true" aria-label="Loading">
      <div className="flex items-center gap-2">
        <Skeleton className="h-6 w-12" />
        <Skeleton className="h-5 w-32" />
        <Skeleton className="ml-auto h-5 w-24" />
      </div>
      <Skeleton className="h-5 w-3/4" />
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} className="h-4 w-full" />
      ))}
    </div>
  );
}
