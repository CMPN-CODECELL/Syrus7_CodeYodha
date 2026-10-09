import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import type { ReactNode, ThHTMLAttributes, TdHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/** Dense terminal-style table primitives. Numbers right-aligned and tabular. */
export function TableShell({ children, className, maxHeight }: { children: ReactNode; className?: string; maxHeight?: string }) {
  return (
    <div className={cn("scrollbar-thin relative w-full overflow-auto", className)} style={maxHeight ? { maxHeight } : undefined}>
      <table className="w-full border-separate border-spacing-0 text-[13px]">{children}</table>
    </div>
  );
}

export function Th({
  children,
  className,
  align = "left",
  ...rest
}: ThHTMLAttributes<HTMLTableCellElement> & { align?: "left" | "right" | "center" }) {
  return (
    <th
      scope="col"
      className={cn(
        "sticky top-0 z-10 h-8 border-b border-hairline bg-surface-1 px-3 text-[10.5px] font-semibold tracking-[0.07em] whitespace-nowrap text-muted-foreground uppercase",
        align === "right" ? "text-right" : align === "center" ? "text-center" : "text-left",
        className,
      )}
      {...rest}
    >
      {children}
    </th>
  );
}

export function SortTh<K extends string>({
  label,
  k,
  sort,
  onSort,
  align = "left",
  className,
}: {
  label: string;
  k: K;
  sort: { key: K; dir: "asc" | "desc" };
  onSort: (k: K) => void;
  align?: "left" | "right";
  className?: string;
}) {
  const active = sort.key === k;
  const Icon = !active ? ArrowUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <Th align={align} className={className} aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
      <button
        type="button"
        onClick={() => onSort(k)}
        className={cn(
          "inline-flex items-center gap-1 rounded uppercase outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
          align === "right" && "flex-row-reverse",
          active && "text-foreground",
        )}
      >
        {label}
        <Icon className={cn("size-3", !active && "opacity-40")} aria-hidden />
      </button>
    </Th>
  );
}

export function Td({
  children,
  className,
  align = "left",
  num: isNum,
  ...rest
}: TdHTMLAttributes<HTMLTableCellElement> & { align?: "left" | "right" | "center"; num?: boolean }) {
  return (
    <td
      className={cn(
        "h-10 border-b border-hairline px-3 whitespace-nowrap",
        align === "right" ? "text-right" : align === "center" ? "text-center" : "text-left",
        isNum && "num",
        className,
      )}
      {...rest}
    >
      {children}
    </td>
  );
}

export function InstrumentCell({ symbol, name, meta }: { symbol: string; name?: string; meta?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col leading-tight">
      <span className="flex items-center gap-1.5 font-medium">
        {symbol}
        {meta}
      </span>
      {name && <span className="max-w-[180px] truncate text-[11px] text-subtle">{name}</span>}
    </div>
  );
}
