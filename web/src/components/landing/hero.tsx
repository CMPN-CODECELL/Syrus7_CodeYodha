import { ArrowDown, Check } from "lucide-react";
import { HeroCanvas } from "./canvases";
import { ExploreButton } from "./cta";
import { HERO_SIGNATURE_POINT } from "./scenes/hero-ribbons";
import { btnSecondary } from "./styles";

const TRUST = ["Open source stack", "Paper account, simulated market", "No paid APIs", "Works without an AI key"];

/** Hairline "circuit" shapes behind the hero, after the reference's faint rounded outlines. */
function Circuit() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 1440 900"
      preserveAspectRatio="xMidYMid slice"
      className="lp-circuit absolute inset-0 hidden size-full opacity-70 [mask-image:radial-gradient(80%_70%_at_30%_40%,#000,transparent)] md:block"
    >
      <rect x="96" y="40" width="430" height="92" rx="46" />
      <path d="M186 132v120M186 252h-90v300a40 40 0 0 0 40 40h60" />
      <circle cx="186" cy="252" r="7" />
      <rect x="610" y="70" width="150" height="250" rx="34" />
      <path d="M760 160h140a30 30 0 0 1 30 30v120" />
      <circle cx="930" cy="310" r="7" />
      <path d="M58 380v420" />
      <circle cx="58" cy="380" r="7" />
      <rect x="1250" y="690" width="150" height="190" rx="34" />
      <circle cx="1250" cy="760" r="7" />
      <path d="M1360 120v400a40 40 0 0 1-40 40h-120" />
      <circle cx="1360" cy="120" r="7" />
    </svg>
  );
}

export function Hero() {
  return (
    <section
      data-scene-host
      aria-labelledby="hero-title"
      className="relative isolate overflow-hidden border-b border-hairline"
    >
      <div aria-hidden className="absolute inset-0 -z-10">
        <HeroCanvas className="absolute inset-0 lp-fade-in" />
        {/* Readability scrim behind the copy and a soft mint floor glow. */}
        <div className="absolute inset-0 bg-[linear-gradient(90deg,var(--background)_0%,color-mix(in_oklch,var(--background)_80%,transparent)_34%,transparent_58%)] max-md:bg-[linear-gradient(180deg,color-mix(in_oklch,var(--background)_72%,transparent)_0%,color-mix(in_oklch,var(--background)_45%,transparent)_75%,transparent)]" />
        <div className="absolute inset-x-0 bottom-0 h-56 bg-[radial-gradient(70%_100%_at_15%_100%,var(--lp-glow),transparent_70%)]" />
        <Circuit />
      </div>

      <div className="mx-auto flex min-h-[calc(100svh-64px)] max-w-[1320px] flex-col justify-center px-4 pt-16 pb-20 sm:px-8 lg:max-h-[940px] lg:min-h-[min(860px,calc(100svh-64px))] lg:px-12 lg:py-24">
        <p className="lp-rise mb-7 inline-flex w-fit flex-wrap items-center gap-x-3 gap-y-1 rounded-2xl border sm:rounded-full border-hairline-strong bg-surface-1/60 px-3.5 py-1.5 text-[12.5px] text-muted-foreground backdrop-blur-md">
          <span className="inline-flex items-center gap-1.5">
            <span className="size-1.5 rounded-full bg-ai" aria-hidden />
            AI: read and draft only
          </span>
          <span className="h-3 w-px bg-hairline-strong max-sm:hidden" aria-hidden />
          <span className="inline-flex items-center gap-1.5">
            <span className="size-1.5 rounded-full bg-human" aria-hidden />
            Orders: your signature only
          </span>
        </p>

        <h1 id="hero-title" className="font-display text-[clamp(3rem,12.2vw,7.4rem)] leading-[1] text-foreground">
          <span className="lp-rise block" style={{ animationDelay: "80ms" }}>
            The AI{" "}
            <span className="underline decoration-ai/60 decoration-dotted decoration-2 underline-offset-[0.14em]">proposes.</span>
          </span>
          <span className="lp-rise block" style={{ animationDelay: "220ms" }}>
            You{" "}
            <span className="relative inline-block text-human">
              countersign.
              <svg
                aria-hidden
                viewBox="0 0 400 24"
                preserveAspectRatio="none"
                className="absolute -bottom-[0.1em] left-0 h-[0.16em] w-[96%]"
              >
                <path
                  d="M3 15 C 70 6, 150 5, 230 9 S 360 17, 397 8"
                  pathLength={1}
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="4"
                  strokeLinecap="round"
                  className="lp-underline lp-underline-delay"
                />
              </svg>
            </span>
          </span>
        </h1>

        <p className="lp-rise mt-8 max-w-[620px] text-[17px] leading-relaxed text-muted-foreground sm:text-[19px]" style={{ animationDelay: "380ms" }}>
          Countersign is an AI trading copilot for 021 Trade, running here on a paper account and a simulated market. Ask in plain
          English and the AI reads your portfolio and drafts the order; a deterministic verifier checks every field, and nothing
          reaches the broker until you sign that exact order on your own device.
        </p>

        <div className="lp-rise mt-10 flex flex-col gap-3 sm:flex-row sm:items-center" style={{ animationDelay: "500ms" }}>
          <ExploreButton className="h-[52px] px-6 text-[15.5px]" />
          <a href="#how-it-works" className={btnSecondary}>
            See how it works
            <ArrowDown className="size-4 transition-transform duration-200 group-hover/btn:translate-y-0.5" aria-hidden />
          </a>
        </div>

        <ul className="lp-rise mt-10 flex flex-wrap gap-x-6 gap-y-2.5 text-[13.5px] text-muted-foreground" style={{ animationDelay: "620ms" }} aria-label="About this build">
          {TRUST.map((t) => (
            <li key={t} className="inline-flex items-center gap-2">
              <span className="grid size-4 place-items-center rounded-full bg-human/15 text-human">
                <Check className="size-2.5" strokeWidth={3} aria-hidden />
              </span>
              {t}
            </li>
          ))}
        </ul>
      </div>

      {/* Label beside the canvas's signature point (positions match the scene's layout). */}
      <div
        aria-hidden
        className="lp-rise pointer-events-none absolute hidden -translate-y-1/2 lg:block"
        style={{
          left: `calc(${HERO_SIGNATURE_POINT.x * 100}% + 26px)`,
          top: `${HERO_SIGNATURE_POINT.y * 100}%`,
          animationDelay: "1400ms",
        }}
      >
        <div className="rounded-lg border border-hairline-strong bg-background/70 px-3 py-2 shadow-2xl backdrop-blur-md">
          <p className="flex items-center gap-1.5 text-[11px] font-medium tracking-wide text-human uppercase">
            <span className="relative flex size-1.5">
              <span className="lp-ping absolute inset-0 rounded-full bg-human" />
              <span className="relative size-1.5 rounded-full bg-human" />
            </span>
            Countersigned
          </p>
          <p className="num mt-1 text-[11.5px] text-muted-foreground">sha256 57578100…1d35c526</p>
          <p className="mt-0.5 text-[11px] text-subtle">one signature, one exact order</p>
        </div>
      </div>
    </section>
  );
}
