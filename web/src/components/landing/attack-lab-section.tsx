import {
  ArrowRight,
  Bot,
  Eye,
  FileX2,
  Repeat,
  Siren,
  Syringe,
  TimerOff,
  TrendingDown,
  Unplug,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { AttackZero } from "./attack-zero";
import { AppLink } from "./cta";
import { Reveal } from "./reveal";
import { Accent, SectionHeading } from "./section-heading";
import { arrowNudge, btnPrimary, container } from "./styles";

// The nine Attack Lab scenarios (docs/ATTACK_LAB.md), each run against the real pipeline.
const SCENARIOS: { id: string; title: string; icon: LucideIcon; guarantee: string }[] = [
  { id: "A", title: "Prompt injection", icon: Syringe, guarantee: "Instrument text is quarantined; a poisoned draft is blocked before it can be signed." },
  { id: "B", title: "Compromised model", icon: Bot, guarantee: "Order tools do not exist for the model, and a forged signature fails verification." },
  { id: "C", title: "Misread order", icon: Eye, guarantee: "You said 100, the draft says 1000: the mismatch is shown and approval is refused." },
  { id: "D", title: "Price drift", icon: TrendingDown, guarantee: "The price moves past your band after signing: the executor sends nothing." },
  { id: "E", title: "Lost broker response", icon: Unplug, guarantee: "Reconcile by tag, find the order, do not resend. Exactly one order." },
  { id: "F", title: "Replay", icon: Repeat, guarantee: "Each approval is single use. Replaying it, or reusing it for a bigger order, is refused." },
  { id: "G", title: "Expired approval", icon: TimerOff, guarantee: "After 60 seconds both the approval service and the executor refuse it." },
  { id: "H", title: "Tampered order", icon: FileX2, guarantee: "Change 5 to 500 in the database and the hash no longer matches. Refused." },
  { id: "I", title: "Out-of-band order", icon: Siren, guarantee: "An order placed with stolen broker keys is caught by the monitor and the kill switch engages." },
];

/** Letters A to I; failed scenarios in red, the rest neutral (or all mint when `allBlocked`). */
export function ScenarioChips({ failed = [], allBlocked = false }: { failed?: string[]; allBlocked?: boolean }) {
  return (
    <ul className="flex flex-wrap gap-1.5" aria-hidden>
      {SCENARIOS.map((s) => (
        <li
          key={s.id}
          className={cn(
            "num grid size-7 place-items-center rounded-md text-[11.5px] font-semibold ring-1 ring-inset",
            allBlocked
              ? "bg-human/12 text-human ring-human/30"
              : failed.includes(s.id)
                ? "bg-danger/12 text-danger ring-danger/30"
                : "bg-foreground/[0.04] text-subtle ring-hairline",
          )}
        >
          {s.id}
        </li>
      ))}
    </ul>
  );
}

export function AttackLabSection() {
  return (
    <section
      id="attack-lab"
      tabIndex={-1}
      aria-labelledby="attack-title"
      className="relative overflow-hidden border-y border-hairline bg-surface-1/25 py-24 outline-none sm:py-32"
    >
      <div aria-hidden className="absolute -top-40 left-1/2 -z-10 h-[520px] w-[900px] -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,var(--lp-glow),transparent)]" />
      <div className={container}>
        <SectionHeading
          id="attack-title"
          label="Attack Lab"
          title={
            <>
              Attacked on purpose. <Accent>Zero</Accent> unapproved trades.
            </>
          }
          lede="The Attack Lab runs nine attacks against the real pipeline, with genuine signatures from a sandboxed test authenticator, and runs the same attacks against a naive copilot for comparison."
        />

        <Reveal className="mt-14 sm:mt-16">
          <div className="grid overflow-hidden rounded-[22px] border border-hairline md:grid-cols-2">
            <AttackZero chips={<ScenarioChips allBlocked />} />
            <div className="relative flex flex-col justify-between gap-6 border-t border-hairline bg-background/40 p-7 sm:p-10 md:border-t-0 md:border-l">
              <p className="text-[13px] font-semibold tracking-wide text-muted-foreground uppercase">Naive baseline</p>
              <div>
                <p className="font-display text-[clamp(6.5rem,17vw,12rem)] leading-[0.85] text-danger/85">5</p>
                <p className="mt-4 max-w-sm text-[15px] leading-relaxed text-muted-foreground">
                  unapproved orders per run from a naive &ldquo;LLM plus confirm button&rdquo; copilot facing the same attacks, as
                  measured in our local runs.
                </p>
              </div>
              <div className="space-y-3">
                <ScenarioChips failed={["A", "B", "C", "E"]} />
                <p className="text-[12.5px] text-subtle">
                  Unapproved orders in A, B, C and E: it bought 10,000 shares from an injected instruction and sent a duplicate
                  after a lost response.
                </p>
              </div>
            </div>
          </div>
        </Reveal>

        <ul className="mt-5 grid gap-px overflow-hidden rounded-[22px] border border-hairline bg-hairline sm:grid-cols-2 lg:grid-cols-3">
          {SCENARIOS.map((s) => (
            <li key={s.id} className="group/sc flex gap-4 bg-background p-5 transition-colors duration-300 hover:bg-surface-1 sm:p-6">
              <span className="relative grid size-10 shrink-0 place-items-center rounded-xl border border-hairline-strong bg-surface-1 text-foreground/80 transition-colors group-hover/sc:border-human/40 group-hover/sc:text-human">
                <s.icon className="size-[18px]" aria-hidden />
                <span className="num absolute -top-1.5 -right-1.5 grid size-4 place-items-center rounded-full bg-surface-3 text-[9px] font-semibold text-muted-foreground">
                  {s.id}
                </span>
              </span>
              <div className="min-w-0">
                <h3 className="flex flex-wrap items-center gap-2 text-[15px] font-semibold">
                  {s.title}
                  <span className="rounded bg-human/12 px-1.5 py-px text-[10px] font-semibold tracking-wide text-human uppercase">Blocked</span>
                </h3>
                <p className="mt-1.5 text-[13.5px] leading-relaxed text-muted-foreground">{s.guarantee}</p>
              </div>
            </li>
          ))}
        </ul>

        <Reveal className="mt-10 flex flex-col items-start gap-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="max-w-xl text-[14.5px] text-muted-foreground">
            Run all nine yourself. A full run takes about 20 seconds and uses sandboxed accounts, never yours.
          </p>
          <AppLink to="/attack-lab" className={cn(btnPrimary)}>
            Run the Attack Lab
            <ArrowRight className={arrowNudge} aria-hidden />
          </AppLink>
        </Reveal>
      </div>
    </section>
  );
}
