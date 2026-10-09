import { FileSignature, GitBranch, Radar, ScanEye, SlidersHorizontal, Timer, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { ApprovalDemo } from "./bento/approval-demo";
import { BandDemo } from "./bento/band-demo";
import { PayloadWindow } from "./bento/payload-window";
import { PlanDemo } from "./bento/plan-demo";
import { ReconcileDemo } from "./bento/reconcile-demo";
import { StandingDemo } from "./bento/standing-demo";
import { Reveal } from "./reveal";
import { Accent, SectionHeading } from "./section-heading";
import { container } from "./styles";

function BentoCard({
  icon: Icon,
  title,
  body,
  children,
  className,
  delay,
  tone = "human",
}: {
  icon: LucideIcon;
  title: string;
  body: string;
  children: ReactNode;
  className?: string;
  delay?: number;
  tone?: "human" | "ai";
}) {
  return (
    <Reveal delay={delay} className={cn("h-full", className)}>
      <article className={cn("lp-card flex h-full flex-col", tone === "ai" && "lp-card-ai")}>
        <div className="p-6 pb-5 sm:p-7 sm:pb-6">
          <span className="grid size-10 place-items-center rounded-xl border border-hairline-strong bg-background/60 text-foreground/85">
            <Icon className="size-[18px]" aria-hidden />
          </span>
          <h3 className="mt-5 text-[21px] font-semibold tracking-tight">{title}</h3>
          <p className="mt-2 max-w-[46ch] text-[15px] leading-relaxed text-muted-foreground">{body}</p>
        </div>
        <div className="flex flex-1 flex-col justify-end px-5 pb-5 sm:px-7 sm:pb-7">{children}</div>
      </article>
    </Reveal>
  );
}

export function ProductBento() {
  return (
    <section id="product" tabIndex={-1} aria-labelledby="product-title" className="relative py-24 outline-none sm:py-32">
      <div className={container}>
        <SectionHeading
          id="product-title"
          label="Product"
          title={
            <>
              Every order is <Accent>exactly</Accent> the one you signed.
            </>
          }
          lede="The AI can only read and draft. Plain, deterministic code turns a draft into an order, and six safeguards stand between that order and the market."
        />

        <div className="mt-14 grid grid-cols-1 gap-4 sm:mt-16 sm:gap-5 lg:grid-cols-12">
          <BentoCard
            className="lg:col-span-7"
            icon={FileSignature}
            title="Exact-order approval"
            body="You sign the order itself, not a chat message. Each field shows where it came from, and the approval lasts 60 seconds."
          >
            <ApprovalDemo />
          </BentoCard>
          <BentoCard
            className="lg:col-span-5"
            delay={80}
            icon={SlidersHorizontal}
            title="Price-band protection"
            body="Your signature covers a price band of 1% around the price you saw. If the market leaves it, the approval is void before anything is sent."
          >
            <BandDemo />
          </BentoCard>

          <BentoCard
            className="lg:col-span-5"
            icon={Radar}
            title="Reconcile before retry"
            body="If the broker takes the order but the reply is lost, the executor looks it up by its unique tag. It never blindly resends."
          >
            <ReconcileDemo />
          </BentoCard>
          <BentoCard
            className="lg:col-span-7"
            delay={80}
            icon={ScanEye}
            title="A signature you can inspect"
            body="Your device signs the SHA-256 hash of a canonical payload: the order's hash, your price band, a one-time nonce and a 60 second expiry."
          >
            <PayloadWindow />
          </BentoCard>

          <BentoCard
            className="lg:col-span-7"
            icon={Timer}
            title="Standing instructions that fire once"
            body="Sign a standing instruction once, with its limits. It fires exactly once when the trigger is hit, even across restarts or concurrent triggers."
          >
            <StandingDemo />
          </BentoCard>
          <BentoCard
            className="lg:col-span-5"
            delay={80}
            icon={GitBranch}
            title="One signature per plan"
            body="Multi-step plans are signed once and cannot change afterwards. If a step does not complete, the steps after it stop. No compensating trades are invented."
          >
            <PlanDemo />
          </BentoCard>
        </div>
      </div>
    </section>
  );
}
