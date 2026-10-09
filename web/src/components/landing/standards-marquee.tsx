import { Binary, Braces, Database, FlaskConical, Hash, KeyRound, Layers, Sigma, Sparkles, Workflow, Zap, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { container, eyebrow } from "./styles";

// Monochrome text marks for the open standards and free software this build runs on.
// Deliberately not logos and not a "trusted by" strip: these are ingredients, not customers.
const MARKS: { label: string; icon: LucideIcon; style: string }[] = [
  { label: "WebAuthn / FIDO2 passkeys", icon: KeyRound, style: "font-semibold tracking-tight" },
  { label: "RFC 8785", icon: Braces, style: "font-mono tracking-tight" },
  { label: "SHA-256", icon: Hash, style: "font-mono font-medium" },
  { label: "PostgreSQL", icon: Database, style: "font-semibold" },
  { label: "Valkey", icon: Layers, style: "font-display text-[22px] italic" },
  { label: "FastAPI", icon: Zap, style: "font-semibold tracking-tight" },
  { label: "Next.js", icon: Binary, style: "font-bold tracking-tighter" },
  { label: "Pydantic AI", icon: Sparkles, style: "font-medium" },
  { label: "DBOS", icon: Workflow, style: "font-mono font-semibold tracking-widest" },
  { label: "TLA+", icon: Sigma, style: "font-display text-[22px]" },
  { label: "Hypothesis", icon: FlaskConical, style: "font-medium italic" },
];

function Track({ hidden }: { hidden?: boolean }) {
  return (
    <ul aria-hidden={hidden || undefined} className="lp-marquee-track flex shrink-0 items-center gap-12 pr-12 sm:gap-16 sm:pr-16">
      {MARKS.map((m) => (
        <li
          key={m.label}
          className="flex shrink-0 items-center gap-2.5 text-[17px] whitespace-nowrap text-foreground/60 transition-colors duration-300 hover:text-foreground"
        >
          <m.icon className="size-[18px] opacity-80" aria-hidden />
          <span className={cn(m.style)}>{m.label}</span>
        </li>
      ))}
    </ul>
  );
}

export function StandardsMarquee() {
  return (
    <section aria-label="Built on open standards" className="border-b border-hairline bg-surface-1/30">
      <div className={cn(container, "flex flex-col gap-5 py-9 lg:flex-row lg:items-center lg:gap-10")}>
        <div className="shrink-0 lg:w-56">
          <p className={eyebrow}>Built on open standards</p>
          <p className="mt-1.5 text-[13px] text-muted-foreground">Open standards and open source services.</p>
        </div>
        <div className="lp-marquee relative flex min-w-0 flex-1 overflow-hidden">
          <Track />
          <Track hidden />
        </div>
      </div>
    </section>
  );
}
