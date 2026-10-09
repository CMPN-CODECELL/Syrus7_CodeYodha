import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { GlobeCanvas } from "./canvases";
import { CountUp } from "./count-up";
import { Reveal } from "./reveal";
import { Accent, SectionHeading } from "./section-heading";
import { container } from "./styles";

// Every figure is a fact about this build (docs/TESTING.md, docs/ATTACK_LAB.md), not a projection.
// Keep TESTS_PASSED equal to the latest "passed" count recorded in docs/TESTING.md.
const TESTS_PASSED = 82;
const STATS: { value: ReactNode; label: string; note: string }[] = [
  { value: <CountUp to={9} />, label: "attack classes blocked", note: "Attack Lab scenarios A to I, 9 of 9" },
  { value: <><CountUp to={60} /><span className="text-[0.45em] text-subtle"> s</span></>, label: "approval window", note: "then the signature expires, unused" },
  { value: <CountUp to={1} />, label: "signature per plan", note: "every step bound to one hash" },
  { value: <CountUp to={TESTS_PASSED} />, label: "automated tests", note: "backend suite, including Hypothesis property-based and stateful tests" },
  { value: <CountUp to={1521} />, label: "distinct states model checked", note: "TLA+ with TLC, no invariant violated" },
  { value: <span className="text-[0.62em]">SHA-256</span>, label: "hash-chained audit", note: "every event linked to the one before" },
];

export function ProofSection() {
  return (
    <section id="proof" aria-labelledby="proof-title" className="relative overflow-hidden py-24 sm:py-32">
      <div className={cn(container, "grid items-center gap-12 lg:grid-cols-12")}>
        <div className="lg:col-span-7">
          <SectionHeading
            id="proof-title"
            label="Proof"
            title={
              <>
                Proof, <Accent>not promises.</Accent>
              </>
            }
            lede="Every number here comes from this build's own test runs and model checks. Nothing is extrapolated."
          />
          <dl className="mt-12 grid grid-cols-2 gap-px overflow-hidden rounded-[22px] border border-hairline bg-hairline sm:grid-cols-3">
            {STATS.map((s, i) => (
              <Reveal key={s.label} delay={i * 60} className="flex flex-col bg-background p-5 sm:p-6">
                <dt className="order-2 mt-3 text-[14px] font-medium">{s.label}</dt>
                <dd className="font-display order-1 text-[clamp(2.6rem,5vw,3.6rem)] leading-none text-foreground">{s.value}</dd>
                <dd className="order-3 mt-1 text-[12.5px] leading-snug text-subtle">{s.note}</dd>
              </Reveal>
            ))}
          </dl>
        </div>
        <div className="relative lg:col-span-5">
          <div aria-hidden className="relative mx-auto aspect-square w-full max-w-[520px]">
            <GlobeCanvas className="absolute inset-0" />
          </div>
          <p className="mt-2 text-center text-[12px] text-subtle">
            Runs on one laptop: every service, the paper broker and the market replay are local. The only outside services are the
            021 Developer Sandbox and, if you choose one, a free cloud AI model.
          </p>
        </div>
      </div>
    </section>
  );
}
