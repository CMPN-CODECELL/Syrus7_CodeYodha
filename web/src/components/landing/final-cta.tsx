import Link from "next/link";
import { cn } from "@/lib/utils";
import { ExploreButton, SignedOutOnly } from "./cta";
import { Reveal } from "./reveal";
import { btnSecondary } from "./styles";

export function FinalCta() {
  return (
    <section aria-labelledby="cta-title" className="relative px-4 py-24 sm:px-6 sm:py-32 lg:px-8">
      <Reveal className="relative mx-auto max-w-[1240px] overflow-hidden rounded-[28px] border border-hairline-strong bg-[radial-gradient(80%_120%_at_50%_120%,color-mix(in_oklch,var(--human)_20%,transparent),transparent_60%),var(--lp-card)] px-6 py-20 text-center sm:px-12 sm:py-28">
        {/* A signature stroke that writes itself across the band. */}
        <svg aria-hidden viewBox="0 0 1200 220" preserveAspectRatio="none" className="pointer-events-none absolute inset-x-0 top-[12%] h-[30%] w-full opacity-60">
          <path
            d="M40 150 C 120 150, 170 50, 240 50 S 300 160, 370 160 S 440 70, 540 84 S 690 170, 810 132 S 1000 58, 1160 96"
            pathLength={1}
            fill="none"
            stroke="var(--human)"
            strokeOpacity="0.35"
            strokeWidth="2"
            strokeLinecap="round"
            className="lp-signature"
          />
        </svg>
        <div className="relative">
          <h2 id="cta-title" className="font-display text-[clamp(2.75rem,8vw,5.5rem)] leading-[0.98]">
            Ready to <span className="text-human">countersign?</span>
          </h2>
          <p className="mx-auto mt-6 max-w-xl text-[17px] leading-relaxed text-muted-foreground">
            Create an account, choose your PIN, register your passkey and place a trade on the simulated market. Nothing here
            touches real money.
          </p>
          <div className="mt-10 flex flex-col items-center justify-center gap-3 sm:flex-row">
            <ExploreButton className="h-[52px] w-full px-6 sm:w-auto" />
            <SignedOutOnly>
              <Link href="/signup?next=/copilot" className={cn(btnSecondary, "h-[52px] w-full sm:w-auto")}>
                Create an account
              </Link>
            </SignedOutOnly>
          </div>
        </div>
      </Reveal>
    </section>
  );
}
