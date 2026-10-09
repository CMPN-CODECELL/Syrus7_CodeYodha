import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Hairline-bordered surface with an optional dense header. */
export function Panel({
  title,
  icon: Icon,
  actions,
  subtitle,
  children,
  className,
  bodyClassName,
  id,
}: {
  title?: ReactNode;
  icon?: LucideIcon;
  actions?: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  id?: string;
}) {
  return (
    <section id={id} className={cn("panel flex min-w-0 flex-col", className)}>
      {(title || actions) && (
        <header className="flex min-h-11 items-center gap-2 border-b border-hairline px-4 py-2">
          {Icon && <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />}
          <div className="min-w-0 flex-1">
            {title && <h2 className="truncate text-[13px] font-semibold tracking-tight">{title}</h2>}
            {subtitle && <p className="truncate text-xs text-muted-foreground">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
        </header>
      )}
      <div className={cn("min-w-0 flex-1", bodyClassName)}>{children}</div>
    </section>
  );
}

export function PageHeader({
  title,
  description,
  icon: Icon,
  actions,
  className,
}: {
  title: string;
  description?: ReactNode;
  icon?: LucideIcon;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-end justify-between gap-3", className)}>
      <div className="flex min-w-0 items-start gap-3">
        {Icon && (
          <div className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-lg bg-surface-2 ring-1 ring-hairline">
            <Icon className="size-[18px] text-muted-foreground" aria-hidden />
          </div>
        )}
        <div className="min-w-0">
          <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
          {description && <p className="mt-0.5 max-w-2xl text-sm text-muted-foreground">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("text-[10.5px] font-semibold tracking-[0.08em] text-muted-foreground uppercase", className)}>{children}</div>
  );
}

/** Scrollable page body inside the shell's main area. */
export function PageContainer({ children, className, wide }: { children: ReactNode; className?: string; wide?: boolean }) {
  return (
    <div className="scrollbar-thin h-full overflow-y-auto">
      <div className={cn("mx-auto w-full space-y-4 px-3 py-4 md:px-6 md:py-6", wide ? "max-w-[1600px]" : "max-w-[1400px]", className)}>
        {children}
      </div>
    </div>
  );
}
