"use client";

import NumberFlow from "@number-flow/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect, useMemo, useState } from "react";
import { ChevronDown, Info, Play, ShieldCheck, ShieldOff, Swords, TriangleAlert, Zap } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { PageContainer, PageHeader } from "@/components/ui/panel";
import { ErrorState, SkeletonCard } from "@/components/ui/states";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/errors";
import { ago, ms } from "@/lib/format";
import { qk, useAttackRuns, useScenarios, useScoreboard } from "@/lib/queries";
import { ACTOR } from "@/lib/status";
import { useUiStore } from "@/lib/store";
import type { AttackRun, Scenario, ScenarioResult, Scoreboard, TranscriptLine } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";

let lastHandledRunNonce = 0;

const OUTCOME_ICON: Record<TranscriptLine["outcome"], { icon: typeof Zap; className: string; label: string }> = {
  attempted: { icon: Zap, className: "text-warning", label: "attempted" },
  blocked: { icon: ShieldCheck, className: "text-profit", label: "blocked" },
  allowed: { icon: ShieldOff, className: "text-danger", label: "allowed" },
  info: { icon: Info, className: "text-muted-foreground", label: "info" },
};

function Transcript({ result, runKey }: { result: ScenarioResult; runKey: string }) {
  const reduce = useReducedMotion();
  return (
    <ol key={runKey} className="space-y-1 rounded-lg bg-background/60 p-2.5 font-mono text-[12px] ring-1 ring-hairline" aria-label={`Transcript for attack ${result.letter}`}>
      {result.transcript.map((l, i) => {
        const a = ACTOR[l.actor];
        const o = OUTCOME_ICON[l.outcome];
        return (
          <motion.li
            key={`${runKey}-${i}`}
            initial={reduce ? false : { opacity: 0, x: -6 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: reduce ? 0 : 0.12 + i * 0.32, duration: 0.2 }}
            className={cn(
              "grid grid-cols-[48px_112px_minmax(0,1fr)_18px] items-start gap-2 rounded px-1.5 py-1",
              l.outcome === "allowed" && "bg-danger/[0.07]",
              l.outcome === "blocked" && "bg-profit/[0.05]",
            )}
          >
            <span className="text-subtle tabular-nums">+{l.t_ms}ms</span>
            <span className={cn("flex items-center gap-1.5 truncate font-semibold", a.className)}>
              <span className={cn("size-1.5 shrink-0 rounded-full", a.dot)} />
              {a.label}
            </span>
            <span className="leading-snug text-foreground/90">{l.step}</span>
            <o.icon className={cn("mt-0.5 size-3.5", o.className)} aria-label={o.label} />
          </motion.li>
        );
      })}
    </ol>
  );
}

function ScenarioCard({
  scenario,
  result,
  runKey,
  running,
  onRun,
  revealDelay,
}: {
  scenario: Scenario;
  result: ScenarioResult | undefined;
  runKey: string;
  running: boolean;
  onRun: () => void;
  revealDelay: number;
}) {
  const reduce = useReducedMotion();
  const [open, setOpen] = useState(false);
  const status = result?.status;
  return (
    <motion.article
      layout={!reduce}
      className={cn(
        "flex flex-col overflow-hidden rounded-xl bg-surface-1 ring-1 transition-colors",
        status === "BLOCKED" ? "ring-profit/25" : status === "FAILED_OPEN" ? "ring-danger/50" : status === "ERROR" ? "ring-warning/40" : "ring-hairline",
      )}
    >
      <div className="flex items-start gap-3 p-4">
        <span className="num grid size-11 shrink-0 place-items-center rounded-lg bg-surface-2 text-xl font-bold ring-1 ring-hairline-strong">
          {scenario.letter}
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-[14px] leading-tight font-semibold">{scenario.title}</h3>
          <div className="mt-1.5">
            <AnimatePresence mode="wait" initial={false}>
              {running ? (
                <motion.span key="run" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                  <Chip tone="info" pulse>
                    Running
                  </Chip>
                </motion.span>
              ) : status ? (
                <motion.span
                  key={`${runKey}-${status}`}
                  initial={reduce ? false : { opacity: 0, scale: 0.85 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ delay: reduce ? 0 : revealDelay, type: "spring", stiffness: 500, damping: 26 }}
                  className="inline-flex"
                >
                  <Chip tone={status === "BLOCKED" ? "profit" : status === "FAILED_OPEN" ? "danger" : "warning"} icon={status === "BLOCKED" ? ShieldCheck : TriangleAlert}>
                    {status === "BLOCKED" ? "Blocked" : status === "FAILED_OPEN" ? "Failed open" : "Error"}
                  </Chip>
                </motion.span>
              ) : (
                <Chip tone="muted">Not run yet</Chip>
              )}
            </AnimatePresence>
          </div>
        </div>
        <Button size="sm" variant="outline" onClick={onRun} disabled={running} aria-label={`Run attack ${scenario.letter}`}>
          <Play /> Run
        </Button>
      </div>
      <dl className="space-y-2.5 px-4 pb-3 text-[12.5px]">
        <div>
          <dt className="text-[10px] font-semibold tracking-[0.08em] text-loss uppercase">Attack</dt>
          <dd className="mt-0.5 leading-snug text-foreground/90">{scenario.attack}</dd>
        </div>
        <div>
          <dt className="text-[10px] font-semibold tracking-[0.08em] text-human uppercase">Expected guarantee</dt>
          <dd className="mt-0.5 leading-snug text-foreground/90">{scenario.expected_guarantee}</dd>
        </div>
        <div>
          <dt className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">Result</dt>
          <dd className="mt-0.5 leading-snug text-muted-foreground">{result ? result.verdict : "Run this scenario to see the verdict."}</dd>
        </div>
      </dl>
      {result && (
        <>
          <div className="mt-auto grid grid-cols-4 border-t border-hairline text-center text-[11px]">
            {[
              ["Attempted", result.attempted_actions, ""],
              ["Blocked", result.blocked_actions, "text-profit"],
              ["Unapproved", result.unapproved_orders, result.unapproved_orders ? "text-danger" : "text-profit"],
              ["Naive", result.naive_unapproved_orders, "text-[oklch(0.75_0.16_55)]"],
            ].map(([k, v, c]) => (
              <div key={k as string} className="border-r border-hairline px-1 py-2 last:border-r-0">
                <div className={cn("num text-[15px] font-semibold", c as string)}>{v}</div>
                <div className="text-subtle">{k}</div>
              </div>
            ))}
          </div>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="flex min-h-10 items-center justify-between gap-2 border-t border-hairline px-4 text-xs text-muted-foreground transition-colors hover:bg-foreground/[0.03] hover:text-foreground"
          >
            <span>
              Transcript · {result.transcript.length} steps · {ms(result.duration_ms)}
            </span>
            <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
          </button>
          <AnimatePresence initial={false}>
            {open && (
              <motion.div
                initial={reduce ? false : { height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={reduce ? undefined : { height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <div className="p-3 pt-0">
                  <Transcript result={result} runKey={runKey} />
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </>
      )}
    </motion.article>
  );
}

function Hero({ sb, running, onRunAll }: { sb: Scoreboard | undefined; running: boolean; onRunAll: () => void }) {
  const now = useNow(10_000);
  const unapproved = sb?.unapproved_executions ?? 0;
  return (
    <section className="relative overflow-hidden rounded-2xl bg-surface-1 ring-1 ring-hairline-strong">
      <div className="grid-bg pointer-events-none absolute inset-0 opacity-40 [mask-image:linear-gradient(to_bottom,black,transparent)]" aria-hidden />
      <div className="relative grid gap-6 p-5 md:p-8 lg:grid-cols-[1.3fr_1fr_0.9fr] lg:items-center">
        <div>
          <p className="text-[11px] font-semibold tracking-[0.14em] text-muted-foreground uppercase">Unapproved executions</p>
          <div className={cn("mt-1 font-mono text-[96px] leading-none font-semibold tracking-tighter tabular-nums md:text-[132px]", unapproved === 0 ? "text-profit" : "text-danger")}>
            <NumberFlow value={unapproved} />
          </div>
          <p className="mt-2 max-w-md text-sm text-muted-foreground">
            Orders that reached the broker without a valid countersignature, counted by the independent invariant monitor across every attack.
          </p>
        </div>
        <div className="rounded-xl bg-danger/[0.06] p-4 ring-1 ring-danger/25">
          <p className="text-[11px] font-semibold tracking-[0.1em] text-danger uppercase">Naive copilot (LLM + confirm button)</p>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="font-mono text-6xl font-semibold tracking-tight text-danger tabular-nums">
              <NumberFlow value={sb?.naive_unapproved_executions ?? 0} />
            </span>
            <span className="text-sm text-danger/80">unapproved</span>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            The same attacks against a sandbox copilot whose model can call place_order behind a generic confirm button.
          </p>
        </div>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2">
            <div className="rounded-lg bg-surface-2/70 p-3 ring-1 ring-hairline">
              <div className="num text-2xl font-semibold">
                <NumberFlow value={sb?.attacks_attempted ?? 0} />
              </div>
              <div className="text-[11px] text-muted-foreground">Attacks attempted</div>
            </div>
            <div className="rounded-lg bg-surface-2/70 p-3 ring-1 ring-hairline">
              <div className="num text-2xl font-semibold text-profit">
                <NumberFlow value={sb?.attacks_blocked ?? 0} />
              </div>
              <div className="text-[11px] text-muted-foreground">Attacks blocked</div>
            </div>
          </div>
          <Button size="lg" className="h-12 w-full bg-danger text-[15px] font-semibold text-white hover:bg-danger/90" onClick={onRunAll} disabled={running}>
            <Swords /> {running ? "Running all attacks..." : "Run all attacks"}
          </Button>
          <p className="text-center text-[11px] text-subtle">
            {sb?.last_run_at ? `Last run ${ago(sb.last_run_at, now || Date.parse(sb.last_run_at))}` : "Not run yet"}
          </p>
        </div>
      </div>
    </section>
  );
}

export function AttackLabView() {
  const qc = useQueryClient();
  const scenarios = useScenarios();
  const runs = useAttackRuns();
  const scoreboard = useScoreboard();
  const nonce = useUiStore((s) => s.attackRunNonce);
  const [overrides, setOverrides] = useState<Record<string, { result: ScenarioResult; runKey: string }>>({});
  const [running, setRunning] = useState<Set<string>>(new Set());

  const onRunDone = (run: AttackRun) => {
    qc.setQueryData(qk.scoreboard, run.scoreboard);
    void qc.invalidateQueries({ queryKey: qk.runs });
    setOverrides((prev) => {
      const next = { ...prev };
      for (const r of run.results) next[r.scenario_id] = { result: r, runKey: run.id };
      return next;
    });
  };

  const runM = useMutation({
    mutationFn: (id: string) => api.runAttack(id),
    onMutate: (id) => setRunning((s) => new Set([...s, id])),
    onSuccess: (run, id) => {
      onRunDone(run);
      const blocked = run.results.filter((r) => r.status === "BLOCKED").length;
      if (id === "all") {
        toast.success(`${blocked} of ${run.results.length} attacks blocked`, {
          description: `Unapproved executions: ${run.scoreboard.unapproved_executions}. Naive copilot: ${run.scoreboard.naive_unapproved_executions}.`,
        });
      }
    },
    onError: (e, id) => toast.error(id === "all" ? "Attack run failed to complete" : `Attack ${id} failed to run`, { description: errorMessage(e) }),
    onSettled: (_d, _e, id) =>
      setRunning((s) => {
        const n = new Set(s);
        n.delete(id);
        return n;
      }),
  });

  useEffect(() => {
    if (nonce > lastHandledRunNonce) {
      lastHandledRunNonce = nonce;
      runM.mutate("all");
    }
  }, [nonce, runM]);

  const latest = runs.data?.[0];
  const resultFor = useMemo(() => {
    const map: Record<string, { result: ScenarioResult; runKey: string }> = {};
    for (const r of latest?.results ?? []) map[r.scenario_id] = { result: r, runKey: latest!.id };
    return { ...map, ...overrides };
  }, [latest, overrides]);

  const allRunning = running.has("all");

  return (
    <PageContainer wide>
      <PageHeader
        icon={Swords}
        title="Attack Lab"
        description="Each scenario attacks a different layer: the prompt, the model, the parser, the market, the broker, and the signature itself. The guarantee must hold every time."
      />
      <Hero sb={scoreboard.data} running={allRunning} onRunAll={() => runM.mutate("all")} />
      {scenarios.error ? (
        <ErrorState error={scenarios.error} title="Attack scenarios unavailable" onRetry={() => void scenarios.refetch()} />
      ) : scenarios.isPending ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      ) : (
        <div className="grid items-start gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[...scenarios.data]
            .sort((a, b) => a.letter.localeCompare(b.letter))
            .map((s, i) => {
              const r = resultFor[s.id];
              return (
                <ScenarioCard
                  key={s.id}
                  scenario={s}
                  result={r?.result}
                  runKey={r?.runKey ?? "none"}
                  running={allRunning || running.has(s.id)}
                  onRun={() => runM.mutate(s.id)}
                  revealDelay={overrides[s.id] ? i * 0.12 : 0}
                />
              );
            })}
        </div>
      )}
    </PageContainer>
  );
}
