import Link from "next/link";
import { Compass } from "lucide-react";

export default function NotFound() {
  return (
    <div className="grid min-h-dvh place-items-center p-6">
      <div className="panel flex max-w-md flex-col items-center gap-3 p-8 text-center">
        <Compass className="size-6 text-muted-foreground" />
        <h1 className="text-base font-semibold">This page does not exist</h1>
        <p className="text-sm text-muted-foreground">The address may be mistyped. Inside the app, every area is reachable from the left rail or with Ctrl K.</p>
        <div className="flex flex-wrap justify-center gap-2">
          <Link href="/copilot" className="inline-flex min-h-10 items-center rounded-lg bg-human px-3 text-sm font-medium text-background">
            Go to the Copilot
          </Link>
          <Link href="/" className="inline-flex min-h-10 items-center rounded-lg px-3 text-sm font-medium text-muted-foreground ring-1 ring-hairline hover:text-foreground">
            Home
          </Link>
        </div>
      </div>
    </div>
  );
}
