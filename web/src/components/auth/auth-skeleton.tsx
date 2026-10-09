import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/** Placeholder card while the auth page reads its ?next= parameter on the client. */
export function AuthCardSkeleton({ wide }: { wide?: boolean }) {
  return (
    <div
      className={cn("w-full rounded-2xl border border-hairline-strong bg-surface-1/85 p-5 sm:p-8", wide ? "max-w-[560px]" : "max-w-[440px]")}
      aria-busy="true"
      aria-label="Loading"
    >
      <Skeleton className="h-3 w-24" />
      <Skeleton className="mt-4 h-9 w-3/4" />
      <Skeleton className="mt-3 h-4 w-full" />
      <div className="mt-8 space-y-4">
        <Skeleton className="h-11 w-full" />
        <Skeleton className="h-11 w-full" />
        <Skeleton className="h-11 w-full" />
      </div>
    </div>
  );
}
