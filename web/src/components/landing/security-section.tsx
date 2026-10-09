import { KeyRound, Link2, Power } from "lucide-react";
import { cn } from "@/lib/utils";
import { LiveStatusPanel } from "./live-status";
import { Reveal } from "./reveal";
import { Accent, SectionHeading } from "./section-heading";
import { container } from "./styles";

const POINTS = [
  {
    icon: KeyRound,
    title: "Credential zones",
    body: "Each process holds only the key it needs. The API that talks to the AI can read; only the executor can write, and it never loads AI code.",
  },
  {
    icon: Power,
    title: "Independent monitor and kill switch",
    body: "A separate process compares the broker's real order book with countersigned approvals. An order without a signature trips the kill switch.",
  },
  {
    icon: Link2,
    title: "Hash-chained audit",
    body: "Every proposal, signature, execution and refusal is appended to a SHA-256 hash chain that can be verified end to end.",
  },
];

export function SecuritySection() {
  return (
    <section id="security" tabIndex={-1} aria-labelledby="security-title" className="relative py-24 outline-none sm:py-32">
      <div aria-hidden className="lp-dots absolute inset-0 -z-10 opacity-40 [mask-image:radial-gradient(60%_60%_at_80%_50%,#000,transparent)]" />
      <div className={cn(container, "grid gap-14 lg:grid-cols-12 lg:gap-12")}>
        <div className="lg:col-span-5">
          <SectionHeading
            id="security-title"
            label="Security"
            title={
              <>
                Security you can <Accent>watch</Accent>, not just trust.
              </>
            }
            lede="The guarantees are enforced by separate processes that do not trust each other, and checked again by one that only watches."
          />
          <Reveal delay={80}>
            <ul className="mt-10 space-y-6">
              {POINTS.map((p) => (
                <li key={p.title} className="flex gap-4">
                  <span className="grid size-10 shrink-0 place-items-center rounded-xl border border-hairline-strong bg-surface-1/70 text-human">
                    <p.icon className="size-[18px]" aria-hidden />
                  </span>
                  <div>
                    <h3 className="text-[16px] font-semibold">{p.title}</h3>
                    <p className="mt-1 text-[14.5px] leading-relaxed text-muted-foreground">{p.body}</p>
                  </div>
                </li>
              ))}
            </ul>
          </Reveal>
        </div>
        <Reveal className="lg:col-span-7 lg:pt-10" delay={100}>
          <LiveStatusPanel />
        </Reveal>
      </div>
    </section>
  );
}
