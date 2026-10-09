"use client";

import { Pause, Play, ShieldCheck } from "lucide-react";
import { useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import { cn } from "@/lib/utils";
import { useInView, usePageVisible, useReducedMotion } from "./hooks";
import { Reveal } from "./reveal";
import { SceneCanvas } from "./scene-canvas";
import { askScene, countersignScene, executeScene, verifyScene } from "./scenes/process";
import { Accent, SectionHeading } from "./section-heading";
import { container } from "./styles";

const STEPS = [
  {
    key: "ask",
    n: "01",
    label: "Ask",
    title: "You ask in plain English",
    happens:
      "Ask about your portfolio or describe a trade. The AI reads your account through a read-only gateway and drafts a proposal for you to review.",
    guarantee: "The AI holds no broker credentials and has no tool that can place, modify, cancel or approve anything.",
    scene: askScene,
    glow: "var(--ai)",
    who: "AI",
  },
  {
    key: "verify",
    n: "02",
    label: "Verify",
    title: "Plain code builds the order",
    happens:
      "A deterministic verifier, not the model, builds the canonical order and checks lot size, tick size, freeze quantity, margin, holdings and the price band. Every field is tagged with its source.",
    guarantee: "If the AI misreads you, for example 1000 shares instead of 100, the mismatch is flagged and the order cannot be approved.",
    scene: verifyScene,
    glow: "var(--info)",
    who: "Verifier",
  },
  {
    key: "countersign",
    n: "03",
    label: "Countersign",
    title: "You sign the exact order",
    happens:
      "You review the order and sign its hash with your device passkey (Windows Hello or Touch ID here). Your app PIN or an authenticator code also works.",
    guarantee: "The approval is bound to that hash, lasts 60 seconds, and is void if the price leaves the band you saw.",
    scene: countersignScene,
    glow: "var(--human)",
    who: "You",
  },
  {
    key: "execute",
    n: "04",
    label: "Execute",
    title: "A separate executor acts once",
    happens:
      "A separate process, the only holder of the broker write key, re-checks the hash, signature, expiry and price band, then sends the order with a unique tag.",
    guarantee: "If the broker's reply is lost, it reconciles by tag before any retry, so one approval executes at most once.",
    scene: executeScene,
    glow: "var(--ai)",
    who: "Executor",
  },
] as const;

const DURATION_MS = 7500;

export function HowItWorks() {
  const [index, setIndex] = useState(0);
  const [hovering, setHovering] = useState(false);
  const [focused, setFocused] = useState(false);
  const [userPaused, setUserPaused] = useState(false);
  const sectionRef = useRef<HTMLElement>(null);
  const inView = useInView(sectionRef, { rootMargin: "-20% 0px" });
  const pageVisible = usePageVisible();
  const reduced = useReducedMotion();
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const autoplay = !reduced && !userPaused;
  const paused = hovering || focused || !inView || !pageVisible;
  const step = STEPS[index];

  const select = (i: number, moveFocus = false) => {
    setIndex(i);
    if (moveFocus) tabRefs.current[i]?.focus();
  };
  const onKey = (e: KeyboardEvent) => {
    const last = STEPS.length - 1;
    const next =
      e.key === "ArrowRight" ? (index === last ? 0 : index + 1)
      : e.key === "ArrowLeft" ? (index === 0 ? last : index - 1)
      : e.key === "Home" ? 0
      : e.key === "End" ? last
      : null;
    if (next === null) return;
    e.preventDefault();
    select(next, true);
  };

  return (
    <section
      ref={sectionRef}
      id="how-it-works"
      tabIndex={-1}
      aria-labelledby="how-title"
      className="relative border-y border-hairline bg-surface-1/25 py-24 outline-none sm:py-32"
    >
      <div className={container}>
        <SectionHeading
          id="how-title"
          label="How it works"
          title={
            <>
              Four steps. <Accent>One signature.</Accent>
            </>
          }
          lede="The AI is an untrusted translator, never an actor. Each step hands a narrower, checkable object to the next."
          aside={
            autoplay || userPaused ? (
              <button
                type="button"
                onClick={() => setUserPaused((p) => !p)}
                className="inline-flex h-10 items-center gap-2 rounded-full border border-hairline-strong px-4 text-[13px] text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
              >
                {userPaused ? <Play className="size-3.5" aria-hidden /> : <Pause className="size-3.5" aria-hidden />}
                {userPaused ? "Resume tour" : "Pause tour"}
              </button>
            ) : null
          }
        />

        <Reveal className="mt-12 sm:mt-16">
          <div
            className="overflow-hidden rounded-[22px] border border-hairline bg-background/50"
            onMouseEnter={() => setHovering(true)}
            onMouseLeave={() => setHovering(false)}
            onFocusCapture={() => setFocused(true)}
            onBlurCapture={() => setFocused(false)}
          >
            <div role="tablist" aria-label="How Countersign works" onKeyDown={onKey} className="grid grid-cols-4 border-b border-hairline">
              {STEPS.map((s, i) => {
                const on = i === index;
                return (
                  <button
                    key={s.key}
                    ref={(el) => {
                      tabRefs.current[i] = el;
                    }}
                    id={`lp-step-tab-${s.key}`}
                    type="button"
                    role="tab"
                    aria-selected={on}
                    aria-controls="lp-step-panel"
                    tabIndex={on ? 0 : -1}
                    onClick={() => {
                      // A click is an explicit choice: stop the tour on that step (Resume tour restarts it).
                      select(i);
                      setUserPaused(true);
                    }}
                    className={cn(
                      "group/tab relative flex flex-col items-center gap-1 px-1 pt-5 pb-4 text-center transition-colors outline-none focus-visible:bg-foreground/[0.04] sm:pt-7 sm:pb-6",
                      i > 0 && "border-l border-hairline",
                    )}
                  >
                    <span
                      className={cn(
                        "font-display text-[34px] leading-none transition-colors duration-300 sm:text-[56px]",
                        on ? "text-foreground" : "text-foreground/25 group-hover/tab:text-foreground/45",
                      )}
                    >
                      {s.n}
                    </span>
                    <span className={cn("text-[12px] font-medium transition-colors sm:text-[14px]", on ? "text-foreground" : "text-muted-foreground")}>
                      {s.label}
                    </span>
                    <span className="absolute inset-x-3 bottom-0 h-[2px] overflow-hidden rounded-full sm:inset-x-8" aria-hidden>
                      {on && (
                        <>
                          {/* A static track always marks the selected step; the tour's progress fills it. */}
                          <span className="absolute inset-0 opacity-35" style={{ background: s.glow }} />
                          {autoplay ? (
                            <span
                              key={`${index}-${userPaused}`}
                              data-paused={paused}
                              onAnimationEnd={() => select((index + 1) % STEPS.length)}
                              className="lp-progress relative block h-full w-full"
                              style={{ "--lp-duration": `${DURATION_MS}ms`, background: s.glow } as CSSProperties}
                            />
                          ) : (
                            <span className="relative block h-full w-full" style={{ background: s.glow }} />
                          )}
                        </>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>

            <div
              id="lp-step-panel"
              role="tabpanel"
              aria-labelledby={`lp-step-tab-${step.key}`}
              className="grid lg:grid-cols-12"
            >
              <div className="relative h-[280px] overflow-hidden sm:h-[380px] lg:col-span-7 lg:h-[460px]">
                <div
                  aria-hidden
                  className="absolute inset-0 transition-[background] duration-700"
                  style={{
                    background: `radial-gradient(70% 80% at 50% 100%, color-mix(in oklch, ${step.glow} 22%, transparent), transparent 70%)`,
                  }}
                />
                <SceneCanvas key={step.key} factory={step.scene} className="lp-fade-in absolute inset-0" staticTime={3} />
                <span className="absolute top-4 left-4 rounded-full border border-hairline-strong bg-background/70 px-2.5 py-1 text-[11px] font-medium text-muted-foreground backdrop-blur">
                  Acting: <span className="text-foreground">{step.who}</span>
                </span>
              </div>
              <div className="flex flex-col justify-center gap-7 border-t border-hairline p-6 sm:p-10 lg:col-span-5 lg:border-t-0 lg:border-l">
                <div key={step.key} className="lp-fade-in space-y-7">
                  <div>
                    <p className="font-mono text-[11px] tracking-[0.2em] text-subtle uppercase">Step {step.n}</p>
                    <h3 className="font-display mt-3 text-[34px] leading-[1.05] sm:text-[40px]">{step.title}</h3>
                  </div>
                  <div>
                    <p className="text-[12px] font-semibold tracking-wide text-muted-foreground uppercase">What happens</p>
                    <p className="mt-2 text-[15.5px] leading-relaxed text-foreground/90">{step.happens}</p>
                  </div>
                  <div className="rounded-xl border border-human/25 bg-human/[0.06] p-4">
                    <p className="flex items-center gap-2 text-[12px] font-semibold tracking-wide text-human uppercase">
                      <ShieldCheck className="size-4" aria-hidden />
                      What is guaranteed
                    </p>
                    <p className="mt-2 text-[15px] leading-relaxed text-foreground/90">{step.guarantee}</p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
