/** Error carrying the backend's machine code and human-readable message. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly detail?: unknown;
  /** Extra top-level fields of the error body, e.g. retry_after_s on LOGIN_LOCKED. */
  readonly extra: Record<string, unknown>;
  constructor(status: number, code: string, message: string, detail?: unknown, extra: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.extra = extra;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Seconds to wait before retrying, from retry_after_s in the error body (or its detail), if any. */
export function retryAfterSeconds(e: unknown): number | null {
  if (!(e instanceof ApiError)) return null;
  const fromDetail = e.detail && typeof e.detail === "object" ? (e.detail as Record<string, unknown>).retry_after_s : undefined;
  const v = e.extra.retry_after_s ?? fromDetail;
  const n = typeof v === "string" ? Number(v) : v;
  return typeof n === "number" && Number.isFinite(n) && n > 0 ? Math.ceil(n) : null;
}

/** Best human-readable message for any thrown value. Never "Something went wrong". */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error && e.message) return e.message;
  return "The request did not complete. Check that the Countersign API is running and try again.";
}

/** Specific guidance for approval error codes from POST /approval/complete and /approval/start. */
export const APPROVAL_ERROR_COPY: Record<string, { title: string; hint: string }> = {
  APPROVAL_EXPIRED: {
    title: "Approval window expired",
    hint: "The 60 s window closed before the signature arrived. Start a fresh window; nothing was executed.",
  },
  PRICE_DRIFT: {
    title: "Price left your approved band",
    hint: "The market moved outside the band you were shown. This approval is void; review the new preview and countersign again.",
  },
  STALE_MARKET_DATA: {
    title: "Market data is stale",
    hint: "The last tick is older than 5 s, so the preview cannot be trusted. Wait for the feed to recover.",
  },
  SIGNATURE_INVALID: {
    title: "Signature could not be verified",
    hint: "The passkey assertion did not match this exact payload. Nothing was executed. Try again with the same device.",
  },
  PAYLOAD_MISMATCH: {
    title: "Payload mismatch",
    hint: "What was signed does not match the order on record. The approval was refused.",
  },
  PIN_INVALID: { title: "Incorrect PIN", hint: "Check your 4-digit app PIN and try again." },
  PIN_LOCKED: {
    title: "PIN locked",
    hint: "Too many incorrect attempts. Use your passkey or authenticator code, or wait for the lock to expire.",
  },
  TOTP_INVALID: {
    title: "Authenticator code rejected",
    hint: "Enter the current 6-digit code from your authenticator app.",
  },
  PROPOSAL_BLOCKED: {
    title: "This proposal is blocked",
    hint: "The verifier found a blocking issue. Revise the instruction instead.",
  },
  ALREADY_APPROVED: {
    title: "Already countersigned",
    hint: "This proposal already has a valid signature. Signatures are single-use and cannot be replayed.",
  },
  KILL_SWITCH_ENGAGED: {
    title: "Kill switch engaged",
    hint: "All execution is halted. Disengage the kill switch in the Security Center (requires your PIN).",
  },
  NO_DEVICE_REGISTERED: {
    title: "No passkey registered",
    hint: "Register this device's passkey, or use your app PIN or authenticator code.",
  },
};
