// Types mirrored from docs/API.md (contract v1). Keep in sync with the backend.

export type Paise = number;
export type Bps = number;
export type ISO = string;

export type Source =
  | "trader_instruction"
  | "instrument_metadata"
  | "live_market_feed"
  | "risk_engine"
  | "account"
  | "system_default"
  | "model_inferred";

export interface InstrumentHandle {
  instrument_id: string;
  exchange: "NSE";
  segment: "EQ" | "FO" | "IDX";
  symbol: string;
  display_name: string;
  isin?: string | null;
  expiry?: string | null;
  strike_paise?: Paise | null;
  option_type?: "CE" | "PE" | null;
  lot_size: number;
  tick_size_paise: Paise;
  flags: string[];
}

export interface Tick {
  instrument_id: string;
  symbol: string;
  ltp_paise: Paise;
  bid_paise: Paise;
  ask_paise: Paise;
  volume: number;
  prev_close_paise: Paise;
  change_paise: Paise;
  change_bps: Bps;
  ts: ISO;
  seq: number;
  stale: boolean;
}

export interface AccountSummary {
  account_id: string;
  day_pnl_paise: Paise;
  total_pnl_paise: Paise;
  cash_paise: Paise;
  available_margin_paise: Paise;
  used_margin_paise: Paise;
  holdings_value_paise: Paise;
  exposure_paise: Paise;
  holdings_count: number;
  positions_count: number;
  open_orders_count: number;
  as_of: ISO;
}

export interface Holding {
  instrument: InstrumentHandle;
  quantity: number;
  avg_price_paise: Paise;
  ltp_paise: Paise;
  prev_close_paise: Paise;
  value_paise: Paise;
  pnl_paise: Paise;
  pnl_bps: Bps;
  day_change_paise: Paise;
  day_change_bps: Bps;
}

export type Product = "CNC" | "MIS" | "NRML" | "INTRADAY";
export type OrderType = "LIMIT" | "MARKET" | "SL" | "SL-M";
export type Side = "BUY" | "SELL";
export type Validity = "DAY" | "IOC";

export interface Position {
  instrument: InstrumentHandle;
  product: Product;
  quantity: number;
  avg_price_paise: Paise;
  ltp_paise: Paise;
  pnl_paise: Paise;
  pnl_bps: Bps;
  day_pnl_paise: Paise;
}

export type BrokerOrderStatus =
  | "OPEN"
  | "TRIGGER_PENDING"
  | "PARTIALLY_FILLED"
  | "COMPLETE"
  | "REJECTED"
  | "CANCELLED";

export interface BrokerOrder {
  broker_order_id: string;
  client_order_id: string | null;
  instrument: InstrumentHandle;
  side: Side;
  quantity: number;
  filled_quantity: number;
  pending_quantity: number;
  order_type: OrderType;
  product: Product;
  validity: Validity;
  limit_price_paise: Paise | null;
  trigger_price_paise: Paise | null;
  protection_price_paise: Paise | null;
  avg_fill_price_paise: Paise | null;
  status: BrokerOrderStatus;
  reject_reason: string | null;
  placed_at: ISO;
  updated_at: ISO;
  /** "external": in the broker's order book but not placed by Countersign (for example from the 021 web app). */
  origin?: "countersign" | "external" | null;
}

// Broker accounts: the 021 Developer Sandbox (linked by the operator) or the local paper broker.

export type BrokerKind = "paper" | "021";
export type MarketDataSource = "live" | "replay";

export interface BrokerAccountStatus {
  connected: boolean;
  token_expires_at: ISO | null;
  market_data: MarketDataSource;
  last_error: string | null;
}

/** GET /api/broker/accounts row. For a 021 account the id is the UCC, which the UI always masks. */
export interface BrokerAccount {
  account_id: string;
  kind: BrokerKind;
  label: string;
  active: boolean;
  status: BrokerAccountStatus;
}

export interface OptionLeg {
  instrument_id: string;
  ltp_paise: Paise;
  oi: number;
  volume: number;
  iv_bps: Bps;
  change_bps: Bps;
}

export interface OptionChainRow {
  strike_paise: Paise;
  ce: OptionLeg;
  pe: OptionLeg;
  is_atm: boolean;
  distance: number;
}

export interface OptionChain {
  underlying: "NIFTY";
  spot_paise: Paise;
  expiry: string;
  expiries: string[];
  atm_strike_paise: Paise;
  strike_step_paise: Paise;
  lot_size: number;
  rows: OptionChainRow[];
  as_of: ISO;
}

// Proposals

export type ProposalKind = "ORDER" | "PLAN" | "MANDATE";
export type ProposalStatus =
  | "BLOCKED"
  | "AWAITING_APPROVAL"
  | "APPROVED"
  | "EXECUTING"
  | "COMPLETED"
  | "PARTIALLY_COMPLETED"
  | "FAILED"
  | "REJECTED_BY_USER"
  | "EXPIRED"
  | "ACTIVE"
  | "CANCELLED"
  | "NEEDS_REVIEW";

export interface Issue {
  code: string;
  severity: "BLOCK" | "WARN" | "INFO";
  message: string;
  field?: string;
}

export interface FieldRow {
  key: string;
  label: string;
  display: string;
  source: Source;
  flagged: boolean;
  note?: string;
}

export interface OrderPreview {
  reference_price_paise: Paise;
  band_low_paise: Paise;
  band_high_paise: Paise;
  band_bps: Bps;
  worst_case_price_paise: Paise;
  order_value_paise: Paise;
  estimated_margin_paise: Paise;
  estimated_charges_paise: Paise;
  charges_breakdown: { label: string; paise: Paise }[];
  current_position_qty: number;
  resulting_position_qty: number;
  available_margin_paise: Paise;
  margin_after_paise: Paise;
  exposure_after_paise: Paise;
  tick_age_ms: number;
  session: { open: boolean; label: string };
}

export type StepState =
  | "PENDING"
  | "SUBMITTED"
  | "OPEN"
  | "PARTIALLY_FILLED"
  | "FILLED"
  | "REJECTED"
  | "CANCELLED"
  | "FAILED"
  | "SKIPPED"
  | "BLOCKED"
  // The broker refused or failed before accepting it and the order is not in its order book: nothing to reconcile.
  | "NOT_PLACED"
  // The executor could not tell whether the broker has the order: never resent, the trader reviews it.
  // The backend calls this NEEDS_REVIEW; AMBIGUOUS is accepted as a synonym.
  | "NEEDS_REVIEW"
  | "AMBIGUOUS";

export interface CanonicalOrder {
  schema_version: 1;
  action: "PLACE" | "MODIFY" | "CANCEL";
  account_id: string;
  instrument: InstrumentHandle;
  side: Side;
  quantity: number;
  order_type: OrderType;
  product: Product;
  validity: Validity;
  limit_price_paise: Paise | null;
  trigger_price_paise: Paise | null;
  protection_price_paise: Paise | null;
  target_broker_order_id: string | null;
  /** MODIFY only: the order as it was when the change was drafted (old values). */
  modify_from?: { quantity: number; order_type: OrderType; limit_price_paise: Paise | null; trigger_price_paise: Paise | null } | null;
}

export interface ProposalStep {
  step_no: number;
  canonical: CanonicalOrder;
  summary: string;
  fields: FieldRow[];
  preview: OrderPreview | null;
  issues: Issue[];
  depends_on: number | null;
  state?: StepState;
  broker_order_id?: string | null;
  client_order_id?: string | null;
  filled_quantity?: number;
  avg_fill_price_paise?: Paise | null;
  reason?: string | null;
  /** Why the plan computed this step, e.g. the rebalance arithmetic for this stock. */
  rationale?: string | null;
}

/** Plans the backend computes from the account (L4). The math is data, formatted by the UI. */
export type PlanKind = "exit_losing_intraday" | "rebalance" | "custom";

export interface PlanFact {
  label: string;
  paise?: Paise | null;
  bps?: Bps | null;
  qty?: number | null;
  text?: string | null;
}

export interface PlanMathColumn {
  key: string;
  label: string;
  format?: "paise" | "bps" | "qty" | "text";
}

export interface PlanDetail {
  kind: PlanKind | (string & {});
  title?: string | null;
  summary?: string | null;
  facts?: PlanFact[];
  columns?: PlanMathColumn[];
  rows?: Record<string, string | number | null>[];
  notes?: string[];
}

export type MandateState =
  | "PENDING_APPROVAL"
  | "ACTIVE"
  | "TRIGGERED"
  | "EXECUTING"
  | "FIRED"
  | "COMPLETED"
  | "NEEDS_REVIEW"
  | "EXPIRED"
  | "CANCELLED"
  | "FAILED";

export interface MandateView {
  mandate_id: string;
  kind: "ORDER" | "ALERT";
  instrument: InstrumentHandle;
  condition_text: string;
  /** DAY_CHANGE_BPS: signed change from the previous close (LTE -300 means down 3% or more today). */
  predicate: { field: "LTP" | "DROP_FROM_AVG_BPS" | "DAY_CHANGE_BPS"; op: "LTE" | "GTE"; threshold: number };
  template: CanonicalOrder | null;
  max_quantity: number | null;
  price_limit_paise: Paise | null;
  one_shot: boolean;
  expires_at: ISO;
  state: MandateState;
  created_at: ISO;
  activated_at?: ISO | null;
  triggered_at?: ISO | null;
  fired_at?: ISO | null;
  trigger_event_id?: string | null;
  last_evaluated_at?: ISO | null;
  reason?: string | null;
  /** Present on GET /api/mandates rows. */
  proposal_id?: string | null;
}

export type ApprovalMethod = "webauthn" | "pin" | "totp";

export interface ApprovalState {
  approval_id: string;
  status: "CHALLENGED" | "VERIFIED" | "FAILED" | "EXPIRED";
  method: ApprovalMethod;
  assurance: "device_signed" | "server_attested";
  issued_at: ISO;
  expires_at: ISO;
  verified_at?: ISO | null;
  credential_label?: string | null;
  payload_hash: string;
  failure_reason?: string | null;
}

export interface ExecutionOutboxRow {
  client_order_id: string;
  step_no: number;
  state: string;
  broker_order_id: string | null;
  attempts: number;
  reconciled: boolean;
}

export interface ExecutionState {
  job_id: string;
  status: "PENDING" | "HELD" | "RUNNING" | "DONE" | "REJECTED" | "HALTED";
  reason?: string | null;
  outbox: ExecutionOutboxRow[];
}

export interface Proposal {
  id: string;
  kind: ProposalKind;
  status: ProposalStatus;
  summary: string;
  canonical_hash: string;
  short_hash: string;
  steps: ProposalStep[];
  mandate: MandateView | null;
  /** STOP_ON_FAILURE: later steps are skipped after a failure. CONTINUE_ON_FAILURE: independent steps, each reported. */
  failure_policy: "STOP_ON_FAILURE" | "CONTINUE_ON_FAILURE" | null;
  /** Present on computed plans (exit losing intraday positions, rebalance). */
  plan?: PlanDetail | null;
  issues: Issue[];
  can_approve: boolean;
  interpretation: {
    user_text: string;
    engine: string;
    disagreements: { field: string; trader_value: string; model_value: string }[];
  };
  approval: ApprovalState | null;
  execution: ExecutionState | null;
  created_at: ISO;
  updated_at: ISO;
}

// Session and devices

export interface Device {
  id: string;
  label: string;
  created_at: ISO;
  last_used_at: ISO | null;
  revoked: boolean;
  is_virtual: boolean;
  transports: string[];
}

export interface Onboarding {
  pin_set: boolean;
  passkeys: number;
  totp: boolean;
  /** True once the required step (the app PIN) is done. */
  complete: boolean;
}

export interface Me {
  user: { id: string; username: string; display_name: string };
  account_id: string;
  devices: Device[];
  has_pin: boolean;
  has_totp: boolean;
  pin_locked_until: ISO | null;
  session: { id: string; created_at: ISO; expires_at: ISO };
  // Added with real sign-up. Optional so an older backend's response still renders.
  email?: string | null;
  account?: { id: string; label: string; kind?: BrokerKind } | null;
  onboarding?: Onboarding | null;
  demo_mode?: boolean;
}

export interface SignupBody {
  display_name: string;
  email: string;
  password: string;
}

export interface LoginBody {
  email: string;
  password: string;
}

export interface PinResetBody {
  password: string;
  new_pin: string;
  totp?: string;
}

/** A fresh proof of an existing approval method, needed before adding or replacing one (STEP_UP_REQUIRED). */
export type StepUpMethod = "pin" | "totp" | "webauthn";

export interface StepUpBody {
  method: StepUpMethod;
  pin?: string;
  code?: string;
  challenge_id?: string;
  credential?: import("@simplewebauthn/browser").AuthenticationResponseJSON;
}

export interface StepUpResult {
  ok: true;
  method: StepUpMethod;
  expires_at: ISO;
}

/** GET /api/public/status: unauthenticated, no per-user data. */
export interface PublicStatus {
  all_operational: boolean;
  systems: { api: boolean; broker: boolean; feed: boolean; executor: boolean; monitor: boolean };
  unapproved_executions: number;
  attacks_attempted: number;
  attacks_blocked: number;
  last_attack_run_at: ISO | null;
  audit_events: number;
  market: { mode: MarketDataSource; nifty: { ltp_paise: Paise; change_bps: Bps } | null };
  generated_at: ISO;
}

export interface SessionInfo {
  id: string;
  created_at: ISO;
  expires_at: ISO;
  user_agent: string;
  current: boolean;
  revoked: boolean;
}

// Chat

export type ChatEngine = "auto" | "ollama" | "gemini" | "groq" | "deterministic";

export type Card =
  | { kind: "account_summary"; data: AccountSummary }
  | { kind: "holdings"; title: string; data: Holding[] }
  | { kind: "positions"; title: string; data: Position[] }
  | { kind: "orders"; title: string; data: BrokerOrder[] }
  | { kind: "quote"; data: { instrument: InstrumentHandle; tick: Tick } }
  | { kind: "option_chain"; data: OptionChain }
  | { kind: "proposal"; proposal_id: string; status: ProposalStatus }
  | { kind: "clarify"; question: string; options: string[] }
  | {
      kind: "notice";
      tone: "info" | "warning" | "danger" | "success";
      title: string;
      message: string;
    };

export type ChatEvent =
  | { type: "meta"; conversation_id: string; engine: string; message_id: string }
  | {
      type: "tool";
      id: string;
      name: string;
      label: string;
      status: "running" | "done" | "error";
      detail?: string;
    }
  | { type: "text-delta"; delta: string }
  | { type: "card"; card: Card }
  | { type: "done" }
  | { type: "error"; code: string; message: string };

export interface ChatHistoryItem {
  id: string;
  role: "user" | "assistant";
  text: string;
  cards: Card[];
  created_at: ISO;
}

export interface ChatRequest {
  conversation_id?: string;
  message: string;
  engine?: ChatEngine;
}

// Approvals

export interface ApprovalStart {
  approval_id: string;
  issued_at: ISO;
  expires_at: ISO;
  payload: Record<string, unknown>;
  payload_hash: string;
  // PublicKeyCredentialRequestOptionsJSON from @simplewebauthn/browser
  webauthn: import("@simplewebauthn/browser").PublicKeyCredentialRequestOptionsJSON | null;
}

export interface ApprovalCompleteBody {
  approval_id: string;
  webauthn?: import("@simplewebauthn/browser").AuthenticationResponseJSON;
  pin?: string;
  totp?: string;
}

export type ApprovalErrorCode =
  | "APPROVAL_EXPIRED"
  | "PRICE_DRIFT"
  | "STALE_MARKET_DATA"
  | "SIGNATURE_INVALID"
  | "PAYLOAD_MISMATCH"
  | "PIN_INVALID"
  | "PIN_LOCKED"
  | "TOTP_INVALID"
  | "PROPOSAL_BLOCKED"
  | "ALREADY_APPROVED"
  | "KILL_SWITCH_ENGAGED"
  | "NO_DEVICE_REGISTERED";

// Outcomes

export type OutcomeKind =
  | "ORDER_ACCEPTED"
  | "ORDER_PARTIALLY_FILLED"
  | "ORDER_FILLED"
  | "ORDER_REJECTED"
  | "ORDER_CANCELLED"
  | "ORDER_NOT_PLACED"
  | "ORDER_NEEDS_REVIEW"
  | "EXTERNAL_ORDER"
  | "EXECUTION_BLOCKED"
  | "RECONCILED"
  | "MANDATE_ARMED"
  | "MANDATE_TRIGGERED"
  | "MANDATE_NEEDS_REVIEW"
  | "ALERT"
  | "PLAN_COMPLETED"
  | "PLAN_PARTIALLY_COMPLETED"
  | "PLAN_FAILED"
  | "APPROVAL_VERIFIED"
  | "KILL_SWITCH";

export type Severity = "info" | "success" | "warning" | "danger";

export interface OutcomeEvent {
  id: string;
  ts: ISO;
  kind: OutcomeKind;
  title: string;
  detail: string;
  severity: Severity;
  proposal_id?: string | null;
  broker_order_id?: string | null;
}

// Audit and security

export interface AuditEvent {
  id: number;
  ts: ISO;
  actor: string;
  event_type: string;
  subject_type: string;
  subject_id: string;
  data: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface AuditVerify {
  ok: boolean;
  checked: number;
  head_hash: string;
  broken_at_id: number | null;
  detail: string;
}

export interface SecurityEvent {
  id: string;
  ts: ISO;
  severity: "info" | "warning" | "critical";
  kind: string;
  message: string;
  data: Record<string, unknown>;
  resolved: boolean;
  /** account: about the caller's own account; system: system-wide; drill: the shared Attack Lab sandbox. */
  scope?: "account" | "system" | "drill";
}

export type SecurityEventScope = "mine" | "all";

export interface MonitorCheck {
  name: string;
  ok: boolean;
  detail: string;
  checked: number;
}

export interface MonitorStatus {
  healthy: boolean;
  last_heartbeat: ISO | null;
  checks: MonitorCheck[];
  violations: number;
  unapproved_executions: number;
}

export interface SecurityStatus {
  kill_switch: { engaged: boolean; reason: string | null; at: ISO | null };
  monitor: MonitorStatus;
  executor: { healthy: boolean; last_heartbeat: ISO | null; holds_write_key: true };
  api: { holds_write_key: false; llm_has_order_tools: false };
  broker: { healthy: boolean; latency_ms: number | null; kind?: BrokerKind; label?: string };
  feed: { healthy: boolean; last_tick_age_ms: number | null; mode: string };
  llm: {
    engine: string;
    available: { ollama: boolean; gemini: boolean; groq: boolean; deterministic: true };
  };
}

// Attack Lab

export interface Scenario {
  id: string;
  letter: string;
  title: string;
  attack: string;
  expected_guarantee: string;
}

export type TranscriptActor =
  | "attacker"
  | "model"
  | "verifier"
  | "approval"
  | "executor"
  | "broker"
  | "monitor"
  | "naive-copilot";

export interface TranscriptLine {
  t_ms: number;
  actor: TranscriptActor;
  step: string;
  outcome: "attempted" | "blocked" | "allowed" | "info";
}

export interface ScenarioResult {
  scenario_id: string;
  letter: string;
  title: string;
  status: "BLOCKED" | "FAILED_OPEN" | "ERROR";
  guarantee: string;
  verdict: string;
  attempted_actions: number;
  blocked_actions: number;
  unapproved_orders: number;
  naive_unapproved_orders: number;
  transcript: TranscriptLine[];
  duration_ms: number;
}

export interface Scoreboard {
  unapproved_executions: number;
  naive_unapproved_executions: number;
  attacks_attempted: number;
  attacks_blocked: number;
  last_run_at: ISO | null;
}

export interface AttackRun {
  id: string;
  started_at: ISO;
  finished_at: ISO;
  results: ScenarioResult[];
  scoreboard: Scoreboard;
}

// Demo controls

export type FeedScenario = "calm" | "dip_to_trigger" | "spike" | "crash" | "stale";
export type BrokerFault =
  | "none"
  | "accept_then_drop"
  | "delay_ack"
  | "reject"
  | "partial_fill"
  | "connection_failure"
  // Mock mode only: the broker answers 503 before accepting (not placed), or times out with no match (needs review).
  | "unavailable_503"
  | "timeout_unmatched";

export interface DemoState {
  feed_mode: string;
  broker_fault: string;
  scenarios: string[];
}

export interface Quote {
  instrument: InstrumentHandle;
  tick: Tick;
}

export interface HistoryPoint {
  time: number;
  value: number;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  detail?: unknown;
}

// Live events from GET /api/events
export type LiveEvent =
  | { type: "tick"; data: Tick }
  | { type: "outcome"; data: OutcomeEvent }
  | { type: "proposal"; data: { id: string; status: ProposalStatus } }
  | { type: "mandate"; data: MandateView }
  | { type: "security"; data: SecurityEvent }
  | { type: "monitor"; data: MonitorStatus }
  | { type: "audit"; data: { id: number; event_type: string; ts: ISO } }
  | { type: "ping"; data: unknown };
