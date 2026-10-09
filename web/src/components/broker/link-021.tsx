"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CircleCheck, Info, Landmark, LoaderCircle, LockKeyhole, Unlink } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { needsStepUp, StepUpInline } from "@/components/approval/setup-inline";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/states";
import { api, IS_MOCK } from "@/lib/api";
import { displayAccountId } from "@/lib/broker";
import { isApiError } from "@/lib/errors";
import { encryptLink021, Link021InputError, LINK021_PASSWORD_MAX, normalizeUcc, uccProblem } from "@/lib/link021";
import { qk, useBrokerAccounts, useBrokerStatus, useUnlink021 } from "@/lib/queries";
import type { BrokerAccount, Link021Result } from "@/lib/types";
import { cn } from "@/lib/utils";

export const LINK021_TITLE = "Connect your 021 account";

const STEP_UP_COPY =
  "Connecting or disconnecting a broker account needs a fresh confirmation with a method you already have. Being signed in is not enough. This confirms who you are and approves no trade.";

const ERROR_TITLE: Record<string, string> = {
  LINK_WRONG_CREDENTIALS: "Wrong UCC or password",
  UCC_LINKED_ELSEWHERE: "This 021 account is linked to another Countersign user",
  UCC_INVALID: "That UCC is not valid",
  INVALID_INPUT: "Check the UCC and password",
  LINK_021_UNAVAILABLE: "021 did not answer, try again",
  LINK_NONCE_INVALID: "The encryption key expired, try again",
  RATE_LIMITED: "Too many attempts",
  LINK_TOO_MANY_FAILURES: "Too many failed attempts for this account",
  LINK_REFUSED: "021 refused the login",
  LINK_EXPIRED: "The connection took too long, try again",
  LINK_FAILED: "Connecting failed, nothing was kept",
  LINK_ROLLBACK_FAILED: "Connecting failed",
  GATEWAY021_OFF: "The 021 gateway is not running",
  GATEWAY021_UNAVAILABLE: "The 021 gateway is not answering",
  PASSWORD_TOO_LONG: "Password too long",
  PASSWORD_REQUIRED: "Enter your 021 password",
  WEBCRYPTO_UNAVAILABLE: "Encryption is not available here",
  LINK_KEY_INVALID: "The 021 gateway key could not be used",
  STEP_UP_REQUIRED: "Choose your app PIN first",
  LINK_UNAVAILABLE: "Not available right now",
};

const UNAVAILABLE_MESSAGE = "Connecting a 021 account is not available on this server right now. Try again later.";

/** A server that lacks the linking endpoints or a gateway that is down: no raw status for the trader. */
function isUnavailable(e: unknown): boolean {
  if (e instanceof Link021InputError) return false;
  if (!isApiError(e)) return true;
  if (e.code === "GATEWAY021_OFF" || e.code === "GATEWAY021_UNAVAILABLE") return true;
  return e.code.startsWith("HTTP_") || e.status === 404 || e.status === 405 || e.status === 501 || e.status === 502 || e.status === 504;
}

/** The error to show: an unavailable server becomes one plain message. */
function friendly(e: unknown, allowNotFound = true): unknown {
  if (!allowNotFound && isApiError(e) && e.code === "ACCOUNT_NOT_FOUND") return e;
  return isUnavailable(e) ? new Link021InputError("LINK_UNAVAILABLE", UNAVAILABLE_MESSAGE) : e;
}

function codeOf(e: unknown): string | null {
  return isApiError(e) || e instanceof Link021InputError ? e.code : null;
}

/** A step-up the trader can do here: the API asked for one and listed at least one method they have. */
function canStepUp(e: unknown): boolean {
  if (!needsStepUp(e)) return false;
  const methods = isApiError(e) ? e.extra.methods : undefined;
  return !Array.isArray(methods) || methods.length > 0;
}

function errorTitle(e: unknown): string {
  const code = codeOf(e);
  return (code && ERROR_TITLE[code]) || "Your 021 account was not connected";
}

/** The masked UCC to show for a linked account. */
export function maskedOf(a: BrokerAccount): string {
  return a.masked_id || displayAccountId(a.account_id, a.kind);
}

/** The trader's own 021 account (linked in the Security Center), if any. */
export function selfLinked021(accounts: BrokerAccount[] | null | undefined): BrokerAccount | null {
  return accounts?.find((a) => a.kind === "021" && a.linked_by === "self") ?? null;
}

/** Fetches a fresh link key, encrypts, and posts. One automatic retry with a new key when the nonce expired or was used. */
async function linkOnce(ucc: string, password: string, onEncrypted: () => void): Promise<Link021Result> {
  for (let attempt = 0; ; attempt++) {
    let key: Awaited<ReturnType<typeof api.link021Key>>;
    try {
      key = await api.link021Key();
    } catch (e) {
      // Without a key nothing is encrypted or sent.
      throw friendly(e);
    }
    const ciphertext = await encryptLink021(key, ucc, password);
    onEncrypted();
    try {
      return await api.link021(ciphertext);
    } catch (e) {
      if (attempt === 0 && isApiError(e) && e.code === "LINK_NONCE_INVALID") continue;
      throw friendly(e);
    }
  }
}

const PANEL_INPUT =
  "h-9 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 text-sm transition-colors outline-none placeholder:text-subtle focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-destructive dark:bg-input/30";

/** What linking does with the password, in plain words. */
export function Link021Explainer({ className }: { className?: string }) {
  return (
    <ul className={cn("space-y-1.5 text-[11.5px] leading-relaxed text-muted-foreground", className)}>
      <li className="flex items-start gap-2">
        <LockKeyhole className="mt-0.5 size-3.5 shrink-0 text-human" aria-hidden />
        <span>
          Your password is encrypted in this browser for the 021 gateway only. The Countersign API only relays the encrypted text and
          cannot read it.
        </span>
      </li>
      <li className="flex items-start gap-2">
        <Info className="mt-0.5 size-3.5 shrink-0 text-info" aria-hidden />
        <span>
          It is stored encrypted on this machine so the gateway can log in again after 021 ends every session at 05:00 IST.
        </span>
      </li>
      <li className="flex items-start gap-2">
        <Info className="mt-0.5 size-3.5 shrink-0 text-info" aria-hidden />
        <span>
          Connecting here logs out other API sessions on that 021 account, because 021 allows one live session per account.
        </span>
      </li>
      <li className="flex items-start gap-2">
        <Unlink className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <span>You can disconnect any time, which deletes the stored password.</span>
      </li>
    </ul>
  );
}

/**
 * UCC and 021 password form. The password input is uncontrolled: it is read once at submit time, cleared right after
 * encrypting and on any error, and never kept in React state, a store or a log. Used in the Security Center and in
 * onboarding.
 */
export function Link021Form({
  variant = "panel",
  onLinked,
  secondary,
  className,
}: {
  variant?: "panel" | "onboarding";
  onLinked?: (res: Link021Result) => void;
  /** Extra actions next to the Connect button, such as "Skip for now" in onboarding. */
  secondary?: ReactNode;
  className?: string;
}) {
  const qc = useQueryClient();
  const pwRef = useRef<HTMLInputElement>(null);
  const [ucc, setUcc] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const onboarding = variant === "onboarding";
  const inputClass = onboarding ? "auth-input" : PANEL_INPUT;

  const clearPassword = () => {
    if (pwRef.current) pwRef.current.value = "";
  };

  const submit = async () => {
    if (busy) return;
    const el = pwRef.current;
    const normalized = normalizeUcc(ucc);
    const problem = uccProblem(normalized);
    if (problem) {
      setError(new Link021InputError("UCC_INVALID", problem));
      return;
    }
    // Read once, at submit time; this local is the only copy and goes away when this function returns.
    let password = el?.value ?? "";
    if (!password) {
      setError(new Link021InputError("PASSWORD_REQUIRED", "Enter the password you use to log in to 021."));
      return;
    }
    setUcc(normalized);
    setBusy(true);
    setError(null);
    let restore = false;
    try {
      const res = await linkOnce(normalized, password, clearPassword);
      if (Array.isArray(res.accounts)) qc.setQueryData(qk.brokerAccounts, res.accounts);
      // Same as switching accounts: portfolio, orders, prices and /api/me follow the new active account.
      void qc.invalidateQueries();
      toast.success("021 account connected", { description: `Connected: ${res.masked_ucc}. It is now your active account.` });
      setUcc("");
      onLinked?.(res);
    } catch (e) {
      // Step-up keeps the password in the field so the retry after confirming can encrypt it again with a new key.
      restore = canStepUp(e);
      setError(e);
    } finally {
      if (restore && el) el.value = password;
      else clearPassword();
      password = "";
      setBusy(false);
    }
  };

  const stepUp = canStepUp(error);
  const code = codeOf(error);
  return (
    <form
      className={cn("space-y-3", className)}
      autoComplete="off"
      noValidate
      aria-label={LINK021_TITLE}
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className={cn("grid gap-3", !onboarding && "sm:grid-cols-2")}>
        <div className="space-y-1.5">
          <label htmlFor="link021-ucc" className={onboarding ? "text-[13px] font-medium" : "text-xs font-medium"}>
            021 client code (UCC)
          </label>
          <input
            id="link021-ucc"
            name="ucc"
            value={ucc}
            onChange={(e) => {
              setUcc(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ""));
              if (error && !stepUp) setError(null);
            }}
            autoComplete="off"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            maxLength={16}
            minLength={4}
            pattern="[A-Za-z0-9]{4,16}"
            required
            disabled={busy}
            aria-invalid={code === "UCC_INVALID" || code === "LINK_WRONG_CREDENTIALS" ? true : undefined}
            aria-describedby="link021-ucc-hint"
            placeholder="ABCD1234"
            className={cn(inputClass, "num uppercase")}
          />
          <p id="link021-ucc-hint" className="text-[11px] text-subtle">
            The client code you log in to 021 with: 4 to 16 letters and digits.
          </p>
        </div>
        <div className="space-y-1.5">
          <label htmlFor="link021-password" className={onboarding ? "text-[13px] font-medium" : "text-xs font-medium"}>
            021 password
          </label>
          <input
            ref={pwRef}
            id="link021-password"
            name="password021"
            type="password"
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            maxLength={LINK021_PASSWORD_MAX}
            required
            disabled={busy}
            data-1p-ignore
            data-lpignore="true"
            aria-invalid={code === "LINK_WRONG_CREDENTIALS" || code === "PASSWORD_REQUIRED" || code === "PASSWORD_TOO_LONG" ? true : undefined}
            className={inputClass}
          />
          {IS_MOCK && <p className="text-[11px] text-warning">Mock mode: any password works except &quot;wrong&quot;.</p>}
        </div>
      </div>

      <Link021Explainer />

      {stepUp ? (
        <StepUpInline error={error} description={STEP_UP_COPY} onVerified={() => void submit()} className="bg-background/60" />
      ) : (
        !!error && <ErrorState error={error} title={errorTitle(error)} compact />
      )}

      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:items-center">
        {secondary}
        <Button
          type="submit"
          disabled={busy}
          className={
            onboarding
              ? "h-11 gap-2 bg-human px-4 text-[15px] font-semibold text-background hover:bg-human/90 sm:ml-auto"
              : "h-9 bg-human text-background hover:bg-human/90"
          }
        >
          {busy ? <LoaderCircle className="animate-spin" aria-hidden /> : <Landmark aria-hidden />}
          {busy ? "Connecting..." : "Connect 021 account"}
        </Button>
      </div>
    </form>
  );
}

/** "Connected: ABCD***E" line shown after a successful link. */
export function Link021Connected({ masked, children, className }: { masked: string; children?: ReactNode; className?: string }) {
  return (
    <div
      role="status"
      className={cn("flex items-start gap-2.5 rounded-lg bg-profit/[0.07] px-3 py-2.5 text-xs ring-1 ring-profit/25", className)}
    >
      <CircleCheck className="mt-0.5 size-4 shrink-0 text-profit" aria-hidden />
      <div className="min-w-0 space-y-0.5">
        <p className="text-sm font-medium text-foreground">
          Connected: <span className="num">{masked}</span>
        </p>
        <div className="leading-relaxed text-muted-foreground">
          {children ?? "Your own 021 account is linked and is now your active account. New drafts and prices use it."}
        </div>
      </div>
    </div>
  );
}

/** Security Center card: connect your own 021 account, or see that it is connected. */
export function Link021Card() {
  const status = useBrokerStatus();
  const accounts = useBrokerAccounts();
  const mine = selfLinked021(accounts.data);
  const enabled = status.data?.gateway021_enabled === true;

  if (status.isPending) {
    return (
      <p className="flex items-center gap-2 px-1 text-xs text-muted-foreground" role="status">
        <LoaderCircle className="size-3.5 animate-spin" aria-hidden /> Checking the 021 gateway...
      </p>
    );
  }

  return (
    <div className="space-y-3 rounded-lg bg-surface-2/50 p-3 ring-1 ring-hairline" id="link-021">
      <div className="flex items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-info/12 text-info ring-1 ring-info/30">
          <Landmark className="size-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-[13.5px] font-semibold">{LINK021_TITLE}</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Trade on your own 021 account with your UCC and 021 password. Orders still need your countersignature.
          </p>
        </div>
      </div>
      {!enabled ? (
        <p className="rounded-md bg-background/50 px-2.5 py-2 text-xs text-muted-foreground ring-1 ring-hairline">
          {status.error
            ? "The broker status is unavailable right now, so a 021 account cannot be connected. Try again in a moment."
            : "The 021 gateway is not running on this installation, so a 021 account cannot be connected here."}
        </p>
      ) : mine ? (
        <Link021Connected masked={maskedOf(mine)}>
          Your own 021 account is linked{mine.active ? " and is your active account" : ""}. Use Disconnect on its card above to
          unlink it and delete the stored password.
        </Link021Connected>
      ) : (
        <Link021Form variant="panel" />
      )}
    </div>
  );
}

/** Disconnect for the trader's own 021 account: confirm, step up when the API asks, then back on paper. */
export function Disconnect021({ account }: { account: BrokerAccount }) {
  const [confirming, setConfirming] = useState(false);
  const unlink = useUnlink021();
  const masked = maskedOf(account);
  const run = () =>
    unlink.mutate(account.account_id, {
      onSuccess: () => {
        setConfirming(false);
        toast.success("021 account disconnected", { description: "The stored 021 password is deleted. You are back on your paper account." });
      },
    });

  if (!confirming) {
    return (
      <Button size="sm" variant="outline" onClick={() => setConfirming(true)}>
        <Unlink aria-hidden /> Disconnect
      </Button>
    );
  }
  return (
    <div className="space-y-2 rounded-md bg-background/50 p-2.5 ring-1 ring-hairline-strong">
      <p className="text-xs leading-snug text-muted-foreground">
        Disconnect your 021 account <span className="num font-medium text-foreground">{masked}</span>? Countersign deletes the stored
        021 password and switches you back to your paper account. Orders you already signed stay bound to the account inside their
        signed hash.
      </p>
      {canStepUp(unlink.error) ? (
        <StepUpInline error={unlink.error} description={STEP_UP_COPY} onVerified={run} className="bg-background/60" />
      ) : (
        unlink.error && (
          <ErrorState
            error={friendly(unlink.error, false)}
            title={
              isApiError(unlink.error) && unlink.error.code === "LINK_OPERATOR_MANAGED"
                ? "Managed by the operator"
                : isUnavailable(unlink.error)
                  ? "Not available right now"
                  : "Not disconnected"
            }
            compact
          />
        )
      )}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="destructive" disabled={unlink.isPending} onClick={run}>
          {unlink.isPending ? <LoaderCircle className="animate-spin" aria-hidden /> : <Unlink aria-hidden />}
          {unlink.isPending ? "Disconnecting..." : "Yes, disconnect"}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={unlink.isPending}
          onClick={() => {
            setConfirming(false);
            unlink.reset();
          }}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
