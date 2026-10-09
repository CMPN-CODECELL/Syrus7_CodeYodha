"use client";

import { RotateCcw, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="grid h-full place-items-center p-6">
      <div role="alert" className="panel flex max-w-lg flex-col gap-3 p-6">
        <div className="flex items-center gap-2">
          <TriangleAlert className="size-5 text-danger" />
          <h1 className="text-base font-semibold">This view failed to render</h1>
        </div>
        <p className="text-sm text-muted-foreground">{error.message || "A rendering error occurred in this view."}</p>
        {error.digest && <p className="num text-xs text-subtle">Reference {error.digest}</p>}
        <p className="text-xs text-muted-foreground">No order was affected: the web app cannot place orders. Reload the view to continue.</p>
        <Button onClick={reset} className="w-fit">
          <RotateCcw /> Reload view
        </Button>
      </div>
    </div>
  );
}
