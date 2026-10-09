"use client";

import { Plus } from "lucide-react";
import { useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Reveal } from "./reveal";
import { Accent, SectionHeading } from "./section-heading";
import { container } from "./styles";

const FAQS: { q: string; a: ReactNode }[] = [
  {
    q: "Is real money at risk?",
    a: "No. Countersign works with the 021 Developer Sandbox, where every account trades virtual money on 021's simulated exchange. New Countersign accounts start on a local paper broker seeded with demo cash and holdings; for the hackathon the operator links the 021 Sandbox account. The Attack Lab always runs on its own paper sandboxes. Nothing reaches a real exchange.",
  },
  {
    q: "What can the AI actually do?",
    a: "Read and draft, nothing else. It can look at your portfolio, quotes and orders through a read-only gateway and draft proposals. It has no broker credentials and no tool that can place, modify, cancel or approve anything.",
  },
  {
    q: "What if the AI is hacked or misbehaves?",
    a: "That is Attack Lab scenario B. A deliberately compromised model tries to call order tools, drafts a 5,000 share market order, forges a signature and claims success. The tools do not exist for it, the draft is blocked, the forged signature fails verification, and the screen shows what the executor actually did.",
  },
  {
    q: "Which AI models does it use?",
    a: "Free tiers of Groq or Gemini, or a local model through Ollama, in a fallback chain you configure. If every model is unavailable, a deterministic interpreter answers. Safety does not depend on the model: the same checks and the same signature apply to every draft.",
  },
  {
    q: "Do I need to install anything?",
    a: "Everything except the optional AI model runs locally on free and open source software: Docker for PostgreSQL and Valkey, Python for the API, executor, paper broker, market replay and monitor, and Node for this web app. The AI can use the free tiers of Groq or Gemini (each needs a free account and API key) or a local Ollama model; with no key at all, a built-in deterministic engine answers. Trading on the 021 Developer Sandbox needs a free 021 developer account; without one, everything runs on the local paper broker. Nothing is paid. Once it is running, all you need is a browser: sign up, choose your PIN and register a passkey.",
  },
  {
    q: "What happens if the broker times out?",
    a: "Every order is written to an outbox before it is sent. If the reply is lost, the executor checks the broker's order book before doing anything else: on the local paper broker by the order's unique tag, on the 021 sandbox (which has no client order tag) by matching the order's details. It never resends on its own. If the order is not there it is marked not placed and you can countersign it again; if the executor cannot tell, the order waits for your review. One approval means at most one order.",
  },
];

function Item({ q, a, i, open, onToggle }: { q: string; a: ReactNode; i: number; open: boolean; onToggle: () => void }) {
  const btn = `lp-faq-q-${i}`;
  const panel = `lp-faq-a-${i}`;
  return (
    <div className="border-b border-hairline">
      <h3>
        <button
          id={btn}
          type="button"
          aria-expanded={open}
          aria-controls={panel}
          onClick={onToggle}
          className="group/faq flex w-full items-center justify-between gap-6 py-6 text-left text-[17px] font-medium transition-colors outline-none hover:text-foreground focus-visible:bg-foreground/[0.03] sm:text-[19px]"
        >
          {q}
          <span
            className={cn(
              "grid size-9 shrink-0 place-items-center rounded-full border border-hairline-strong text-muted-foreground transition-[transform,background-color,color] duration-300 group-hover/faq:text-foreground",
              open && "rotate-45 bg-foreground text-background group-hover/faq:text-background",
            )}
            aria-hidden
          >
            <Plus className="size-4" />
          </span>
        </button>
      </h3>
      <div
        id={panel}
        role="region"
        aria-labelledby={btn}
        inert={!open}
        className={cn("grid transition-[grid-template-rows,opacity] duration-300 ease-out", open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0")}
      >
        <div className="overflow-hidden">
          <p className="max-w-3xl pb-7 text-[15.5px] leading-relaxed text-muted-foreground">{a}</p>
        </div>
      </div>
    </div>
  );
}

export function Faq() {
  const [open, setOpen] = useState<number | null>(0);
  return (
    <section id="faq" tabIndex={-1} aria-labelledby="faq-title" className="relative py-24 outline-none sm:py-32">
      <div className={cn(container, "grid gap-12 lg:grid-cols-12")}>
        <div className="lg:col-span-4">
          <SectionHeading
            id="faq-title"
            label="FAQ"
            title={
              <>
                Questions, <Accent>answered.</Accent>
              </>
            }
            lede="The short version: the AI drafts, you decide, and this build never touches real money."
          />
        </div>
        <Reveal className="border-t border-hairline lg:col-span-8">
          {FAQS.map((f, i) => (
            <Item key={f.q} q={f.q} a={f.a} i={i} open={open === i} onToggle={() => setOpen((o) => (o === i ? null : i))} />
          ))}
        </Reveal>
      </div>
    </section>
  );
}
