// Single source of truth for how states, sources and events look across the UI.

import {
  Activity,
  Ban,
  BookCopy,
  Bell,
  BookOpen,
  Calculator,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleSlash,
  CircleX,
  Clock,
  Crosshair,
  Hourglass,
  ListChecks,
  ListTodo,
  ListX,
  Lock,
  OctagonX,
  PenLine,
  Power,
  RefreshCcwDot,
  Send,
  Settings2,
  ShieldX,
  Signature,
  Sparkles,
  TriangleAlert,
  User,
  Wallet,
  Zap,
  type LucideIcon,
} from "lucide-react";
import type {
  BrokerOrderStatus,
  MandateState,
  OutcomeKind,
  ProposalStatus,
  Severity,
  Source,
  StepState,
  TranscriptLine,
} from "./types";
import { humanize } from "./format";

export type Tone = "neutral" | "muted" | "profit" | "loss" | "warning" | "danger" | "info" | "ai" | "human";

export interface Look {
  label: string;
  tone: Tone;
  icon: LucideIcon;
  pulse?: boolean;
}

export const PROPOSAL_STATUS: Record<ProposalStatus, Look> = {
  BLOCKED: { label: "Blocked", tone: "danger", icon: Lock },
  AWAITING_APPROVAL: { label: "Awaiting your countersignature", tone: "human", icon: PenLine, pulse: true },
  APPROVED: { label: "Countersigned", tone: "human", icon: Signature },
  EXECUTING: { label: "Executing", tone: "info", icon: Activity, pulse: true },
  COMPLETED: { label: "Completed", tone: "profit", icon: CircleCheck },
  PARTIALLY_COMPLETED: { label: "Partially completed", tone: "warning", icon: CircleDashed },
  FAILED: { label: "Failed", tone: "danger", icon: CircleX },
  REJECTED_BY_USER: { label: "Rejected by you", tone: "muted", icon: Ban },
  EXPIRED: { label: "Expired", tone: "muted", icon: Hourglass },
  ACTIVE: { label: "Armed", tone: "human", icon: Crosshair },
  CANCELLED: { label: "Cancelled", tone: "muted", icon: Ban },
  NEEDS_REVIEW: { label: "Needs your review", tone: "warning", icon: TriangleAlert },
};

export const STEP_STATE: Record<StepState, Look> = {
  PENDING: { label: "Pending", tone: "muted", icon: Clock },
  SUBMITTED: { label: "Submitted", tone: "info", icon: Send, pulse: true },
  OPEN: { label: "Open", tone: "info", icon: CircleDot, pulse: true },
  PARTIALLY_FILLED: { label: "Partially filled", tone: "warning", icon: CircleDashed },
  FILLED: { label: "Filled", tone: "profit", icon: CircleCheck },
  REJECTED: { label: "Rejected", tone: "danger", icon: CircleX },
  CANCELLED: { label: "Cancelled", tone: "muted", icon: Ban },
  FAILED: { label: "Failed", tone: "danger", icon: OctagonX },
  SKIPPED: { label: "Skipped", tone: "muted", icon: Ban },
  BLOCKED: { label: "Blocked", tone: "danger", icon: Lock },
  NOT_PLACED: { label: "Not placed", tone: "danger", icon: CircleSlash },
  NEEDS_REVIEW: { label: "Needs your review", tone: "warning", icon: TriangleAlert },
  AMBIGUOUS: { label: "Needs your review", tone: "warning", icon: TriangleAlert },
};

export const MANDATE_STATE: Record<MandateState, Look> = {
  PENDING_APPROVAL: { label: "Needs countersign", tone: "human", icon: PenLine },
  ACTIVE: { label: "Active", tone: "human", icon: Crosshair, pulse: true },
  TRIGGERED: { label: "Triggered", tone: "warning", icon: Zap, pulse: true },
  EXECUTING: { label: "Executing", tone: "info", icon: Activity, pulse: true },
  FIRED: { label: "Fired", tone: "profit", icon: CircleCheck },
  COMPLETED: { label: "Completed", tone: "profit", icon: CircleCheck },
  NEEDS_REVIEW: { label: "Needs review", tone: "warning", icon: TriangleAlert },
  EXPIRED: { label: "Expired", tone: "muted", icon: Hourglass },
  CANCELLED: { label: "Cancelled", tone: "muted", icon: Ban },
  FAILED: { label: "Failed", tone: "danger", icon: CircleX },
};

export const ORDER_STATUS: Record<BrokerOrderStatus, Look> = {
  OPEN: { label: "Open", tone: "info", icon: CircleDot },
  TRIGGER_PENDING: { label: "Trigger pending", tone: "info", icon: Clock },
  PARTIALLY_FILLED: { label: "Partial", tone: "warning", icon: CircleDashed },
  COMPLETE: { label: "Complete", tone: "profit", icon: CircleCheck },
  REJECTED: { label: "Rejected", tone: "danger", icon: CircleX },
  CANCELLED: { label: "Cancelled", tone: "muted", icon: Ban },
};

export const OUTCOME_KIND: Record<OutcomeKind, { label: string; icon: LucideIcon }> = {
  ORDER_ACCEPTED: { label: "Accepted", icon: Send },
  ORDER_PARTIALLY_FILLED: { label: "Partial fill", icon: CircleDashed },
  ORDER_FILLED: { label: "Filled", icon: CircleCheck },
  ORDER_REJECTED: { label: "Rejected", icon: CircleX },
  ORDER_CANCELLED: { label: "Cancelled", icon: Ban },
  ORDER_NOT_PLACED: { label: "Not placed", icon: CircleSlash },
  ORDER_NEEDS_REVIEW: { label: "Needs your review", icon: TriangleAlert },
  EXTERNAL_ORDER: { label: "Outside Countersign", icon: BookCopy },
  EXECUTION_BLOCKED: { label: "Blocked", icon: ShieldX },
  RECONCILED: { label: "Reconciled", icon: RefreshCcwDot },
  MANDATE_ARMED: { label: "Mandate armed", icon: Crosshair },
  MANDATE_TRIGGERED: { label: "Mandate triggered", icon: Zap },
  MANDATE_NEEDS_REVIEW: { label: "Needs review", icon: TriangleAlert },
  ALERT: { label: "Alert", icon: Bell },
  PLAN_COMPLETED: { label: "Plan completed", icon: ListChecks },
  PLAN_PARTIALLY_COMPLETED: { label: "Plan partial", icon: ListTodo },
  PLAN_FAILED: { label: "Plan failed", icon: ListX },
  APPROVAL_VERIFIED: { label: "Countersigned", icon: Signature },
  KILL_SWITCH: { label: "Kill switch", icon: Power },
};

export const OUTCOME_GROUPS: { id: string; label: string; kinds: OutcomeKind[] }[] = [
  { id: "orders", label: "Orders", kinds: ["ORDER_ACCEPTED", "ORDER_PARTIALLY_FILLED", "ORDER_FILLED", "ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_NOT_PLACED", "ORDER_NEEDS_REVIEW", "EXTERNAL_ORDER", "RECONCILED"] },
  { id: "mandates", label: "Mandates", kinds: ["MANDATE_ARMED", "MANDATE_TRIGGERED", "MANDATE_NEEDS_REVIEW", "ALERT"] },
  { id: "plans", label: "Plans", kinds: ["PLAN_COMPLETED", "PLAN_PARTIALLY_COMPLETED", "PLAN_FAILED"] },
  { id: "security", label: "Approvals and security", kinds: ["APPROVAL_VERIFIED", "EXECUTION_BLOCKED", "KILL_SWITCH"] },
];

// Safe lookups: a state the backend adds later renders as a neutral chip instead of crashing the card.

function fallbackLook(state: string | null | undefined): Look {
  return { label: humanize(state || "unknown"), tone: "muted", icon: CircleDashed };
}

export function proposalLook(s: string | null | undefined): Look {
  return PROPOSAL_STATUS[s as ProposalStatus] ?? fallbackLook(s);
}

export function stepLook(s: string | null | undefined): Look {
  return STEP_STATE[(s || "PENDING") as StepState] ?? fallbackLook(s);
}

export function orderLook(s: string | null | undefined): Look {
  return ORDER_STATUS[s as BrokerOrderStatus] ?? fallbackLook(s);
}

export function mandateLook(s: string | null | undefined): Look {
  return MANDATE_STATE[s as MandateState] ?? fallbackLook(s);
}

export const SEVERITY_TONE: Record<Severity, Tone> = {
  info: "info",
  success: "profit",
  warning: "warning",
  danger: "danger",
};

export interface SourceLook {
  label: string;
  icon: LucideIcon;
  tone: Tone;
  explain: string;
}

export const SOURCE: Record<Source, SourceLook> = {
  trader_instruction: {
    label: "Your instruction",
    icon: User,
    tone: "human",
    explain: "This value appears in your own words. The deterministic parser and the model both read it from your message.",
  },
  instrument_metadata: {
    label: "Instrument metadata",
    icon: BookOpen,
    tone: "neutral",
    explain: "From the exchange instrument master: lot size, tick size, freeze quantity and circuit limits.",
  },
  live_market_feed: {
    label: "Live market feed",
    icon: Activity,
    tone: "info",
    explain: "From the live tick feed at preview time. If the price leaves the band, the approval is void.",
  },
  risk_engine: {
    label: "Risk engine",
    icon: Calculator,
    tone: "neutral",
    explain: "Computed by the verifier: margin, charges, protection price and the approval price band.",
  },
  account: {
    label: "Account",
    icon: Wallet,
    tone: "neutral",
    explain: "From your broker account through the read-only gateway (holdings, funds, positions).",
  },
  system_default: {
    label: "System default",
    icon: Settings2,
    tone: "muted",
    explain: "A safe default applied because you did not specify it, such as CNC delivery and DAY validity.",
  },
  model_inferred: {
    label: "AI inferred",
    icon: Sparkles,
    tone: "ai",
    explain: "The AI proposed this value but you never stated it. AI-inferred values are always flagged for your attention.",
  },
};

export const ACTOR: Record<TranscriptLine["actor"], { label: string; className: string; dot: string }> = {
  attacker: { label: "Attacker", className: "text-loss", dot: "bg-loss" },
  model: { label: "Model", className: "text-ai", dot: "bg-ai" },
  verifier: { label: "Verifier", className: "text-info", dot: "bg-info" },
  approval: { label: "Approval", className: "text-human", dot: "bg-human" },
  executor: { label: "Executor", className: "text-[oklch(0.78_0.11_195)]", dot: "bg-[oklch(0.78_0.11_195)]" },
  broker: { label: "Broker", className: "text-[oklch(0.72_0.03_250)]", dot: "bg-[oklch(0.72_0.03_250)]" },
  monitor: { label: "Monitor", className: "text-warning", dot: "bg-warning" },
  "naive-copilot": { label: "Naive copilot", className: "text-[oklch(0.75_0.16_55)]", dot: "bg-[oklch(0.75_0.16_55)]" },
};
