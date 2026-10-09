"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Check, ChevronDown, CircleAlert, Hash as HashIcon, KeyRound, LockKeyhole, RotateCcw, Smartphone } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Hash } from "@/components/ui/hash";
import { SideBadge } from "@/components/ui/chip";
import { Skeleton } from "@/components/ui/skeleton";
import { api, IS_MOCK } from "@/lib/api";
import { displayAccountId } from "@/lib/broker";
import { ApiError, APPROVAL_ERROR_COPY, errorMessage, isApiError } from "@/lib/errors";
import { chunkHash } from "@/lib/format";
import { MOCK_TOTP } from "@/lib/mock";
import { qk, useMe } from "@/lib/queries";
import type { ApprovalCompleteBody, ApprovalMethod, Proposal } from "@/lib/types";
import { cn } from "@/lib/utils";
import { PasskeyError, signWithPasskey } from "@/lib/webauthn";
import { CountdownRing, useWindowExpired } from "./countdown-ring";
import { PinSlots, RegisterPasskeyInline, SetPinInline } from "./setup-inline";

const RESTARTABLE = new Set(["APPROVAL_EXPIRED", "PRICE_DRIFT", "STALE_MARKET_DATA", "SIGNATURE_INVALID", "PAYLOAD_MISMATCH"]);

const METHODS: { id: ApprovalMethod; label: string; icon: typeof KeyRound }[] = [
  { id: "webauthn", label: "Passkey", icon: KeyRound },
  { id: "pin", label: "App PIN", icon: LockKeyhole },
  { id: "totp", label: "Authenticator code", icon: Smartphone },
];

function errorCode(e: unknown): string | null {
  if (isApiError(e)) return e.code;
  if (e instanceof PasskeyError) return e.code;
  return null;
}

function ApprovalError({ error, onRestart }: { error: unknown; onRestart?: () => void }) {
  const code = errorCode(error);
  const copy = code ? APPROVAL_ERROR_COPY[code] : undefined;
  return (
    <div role="alert" className="flex items-start gap-2.5 rounded-lg bg-danger/[0.08] p-3 ring-1 ring-danger/30">
      <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
      <div className="min-w-0 flex-1 space-y-1 text-xs">
        <p className="text-sm font-medium text-danger">{copy?.title ?? "Countersignature not accepted"}</p>
        <p className="text-foreground/90">{errorMessage(error)}</p>
        {copy && <p className="text-muted-foreground">{copy.hint}</p>}
        {code && <p className="num text-[10.5px] text-subtle">{code}</p>}
        {onRestart && code && RESTARTABLE.has(code) && (
          <Button size="sm" variant="outline" className="mt-1" onClick={onRestart}>
            <RotateCcw /> Start a new 60 s window
          </Button>
        )}
      </div>
    </div>
  );
}

function WhatYouSign({ proposal }: { proposal: Proposal }) {
  if (proposal.kind === "PLAN") {
    return (
      <ol className="space-y-1.5">
        {proposal.steps.map((s) => (
          <li key={s.step_no} className="flex items-start gap-2">
            <span className="num mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-surface-3 text-[11px]">{s.step_no}</span>
            <span className="num text-[13px] leading-snug">
              {s.summary}
              {s.depends_on ? <span className="text-muted-foreground"> (after step {s.depends_on} fills)</span> : null}
            </span>
          </li>
        ))}
      </ol>
    );
  }
  if (proposal.kind === "MANDATE" && proposal.mandate) {
    return (
      <div className="space-y-1">
        <p className="num text-[13px]">
          WHEN <span className="font-semibold">{proposal.mandate.condition_text}</span> on {proposal.mandate.instrument.symbol}
        </p>
        <p className="num text-[13px] text-muted-foreground">
          {proposal.steps[0]?.summary ? `THEN ${proposal.steps[0].summary}` : "THEN notify me (alert only, never trades)"}
        </p>
      </div>
    );
  }
  const s = proposal.steps[0];
  return (
    <div className="flex items-start gap-2">
      {s && <SideBadge side={s.canonical.side} size="sm" className="mt-0.5" />}
      <p className="num text-[14px] leading-snug font-semibold">{s?.summary ?? proposal.summary}</p>
    </div>
  );
}

function CountersignFlow({ proposal, onClose }: { proposal: Proposal; onClose: () => void }) {
  const qc = useQueryClient();
  const me = useMe().data;
  const reduce = useReducedMotion();
  const activeDevices = me?.devices.filter((d) => !d.revoked) ?? [];
  const defaultMethod: ApprovalMethod = activeDevices.length ? "webauthn" : me?.has_pin ? "pin" : me?.has_totp ? "totp" : "webauthn";
  const [method, setMethod] = useState<ApprovalMethod>(defaultMethod);
  const [step, setStep] = useState<"confirm" | "auth" | "done">("confirm");
  const [pin, setPin] = useState("");
  const [totp, setTotp] = useState("");
  const [signing, setSigning] = useState(false);
  const [registerHere, setRegisterHere] = useState(false);
  const [localError, setLocalError] = useState<unknown>(null);
  const started = useRef(false);

  const startM = useMutation({ mutationFn: (m: ApprovalMethod) => api.approvalStart(proposal.id, m) });
  const completeM = useMutation({
    mutationFn: (body: ApprovalCompleteBody) => api.approvalComplete(proposal.id, body),
    onSuccess: (p) => {
      qc.setQueryData(qk.proposal(p.id), p);
      void qc.invalidateQueries({ queryKey: qk.proposalsAll });
      void qc.invalidateQueries({ queryKey: qk.plans });
      void qc.invalidateQueries({ queryKey: qk.mandates });
      void qc.invalidateQueries({ queryKey: qk.me });
      setStep("done");
      toast.success(p.kind === "MANDATE" ? "Mandate armed" : "Countersigned", {
        description:
          p.kind === "MANDATE"
            ? "Signature verified. It fires once when the condition is met on a fresh tick."
            : "Signature verified. A single-use capability was minted; the executor takes it from here.",
      });
      setTimeout(onClose, 1700);
    },
    onError: (e) => {
      if (errorCode(e) === "PIN_INVALID") setPin("");
      if (errorCode(e) === "TOTP_INVALID") setTotp("");
    },
  });

  const start = startM.data;
  const expired = useWindowExpired(start?.expires_at);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    startM.mutate(defaultMethod);
  }, [startM, defaultMethod]);

  const restart = (m: ApprovalMethod = method) => {
    setLocalError(null);
    completeM.reset();
    setPin("");
    setTotp("");
    startM.mutate(m);
  };

  const needsSetup = (m: ApprovalMethod) =>
    (m === "webauthn" && activeDevices.length === 0) || (m === "pin" && !me?.has_pin) || (m === "totp" && !me?.has_totp);

  const switchMethod = (m: ApprovalMethod) => {
    if (m === method) return;
    setMethod(m);
    if (needsSetup(m)) {
      // Nothing to sign with yet: the panel offers setup, and a fresh window starts once it is done.
      setLocalError(null);
      completeM.reset();
      return;
    }
    restart(m);
  };

  const signPasskey = async () => {
    if (!start?.webauthn) return;
    setLocalError(null);
    completeM.reset();
    setSigning(true);
    try {
      const assertion = await signWithPasskey(start.webauthn);
      completeM.mutate({ approval_id: start.approval_id, webauthn: assertion });
    } catch (e) {
      setLocalError(e);
    } finally {
      setSigning(false);
    }
  };

  const submitPin = (v = pin) => {
    if (!start || v.length !== 4 || completeM.isPending) return;
    completeM.mutate({ approval_id: start.approval_id, pin: v });
  };
  const submitTotp = (v = totp) => {
    if (!start || v.length !== 6 || completeM.isPending) return;
    completeM.mutate({ approval_id: start.approval_id, totp: v });
  };

  const busy = startM.isPending || completeM.isPending || signing;
  const error = localError ?? completeM.error ?? startM.error;
  const kindLabel = proposal.kind === "PLAN" ? "plan" : proposal.kind === "MANDATE" ? "standing instruction" : "order";

  if (step === "done") {
    return (
      <div className="flex flex-col items-center gap-3 px-2 py-8 text-center">
        <motion.div
          initial={reduce ? false : { scale: 0.6, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ type: "spring", stiffness: 380, damping: 22 }}
          className="grid size-16 place-items-center rounded-full bg-human text-background shadow-[0_0_0_8px_color-mix(in_oklch,var(--human)_18%,transparent)]"
        >
          <Check className="size-8" strokeWidth={3} />
        </motion.div>
        <DialogTitle className="text-lg font-semibold">{proposal.kind === "MANDATE" ? "Mandate armed" : "Countersigned"}</DialogTitle>
        <DialogDescription className="max-w-sm text-sm">
          {proposal.kind === "MANDATE"
            ? "Your signature is verified. The mandate is one-shot and will not fire on stale prices."
            : "Signature verified. A single-use capability was minted and handed to the executor, which re-verifies the hash, expiry and price band before sending one tagged order."}
        </DialogDescription>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex items-start gap-3 pr-8">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-[11px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
            <span className={cn("flex items-center gap-1.5", step === "confirm" && "text-human")}>
              <span className={cn("num grid size-4 place-items-center rounded-full text-[10px]", step === "confirm" ? "bg-human text-background" : "bg-surface-3")}>
                1
              </span>
              Confirm
            </span>
            <span className="h-px w-6 bg-hairline-strong" />
            <span className={cn("flex items-center gap-1.5", step === "auth" && "text-human")}>
              <span className={cn("num grid size-4 place-items-center rounded-full text-[10px]", step === "auth" ? "bg-human text-background" : "bg-surface-3")}>
                2
              </span>
              Authenticate
            </span>
          </div>
          <DialogTitle className="mt-2 text-base font-semibold">
            {step === "confirm" ? `Confirm this exact ${kindLabel}` : "Authenticate to countersign"}
          </DialogTitle>
          <DialogDescription className="mt-0.5 text-xs">
            {step === "confirm"
              ? "This is the canonical object the verifier checked. You sign its hash, nothing else."
              : "Your signature is single-use, bound to the hash below, and expires with the approval window."}
          </DialogDescription>
        </div>
        {start ? (
          <CountdownRing issuedAt={start.issued_at} expiresAt={start.expires_at} size={step === "confirm" ? 64 : 52} />
        ) : (
          <Skeleton className={cn("shrink-0 rounded-full", step === "confirm" ? "size-16" : "size-13")} />
        )}
      </div>

      <AnimatePresence mode="wait" initial={false}>
        {step === "confirm" ? (
          <motion.div
            key="confirm"
            initial={reduce ? false : { opacity: 0, x: 12 }}
            animate={{ opacity: 1, x: 0 }}
            exit={reduce ? undefined : { opacity: 0, x: -12 }}
            transition={{ duration: 0.18 }}
            className="space-y-3"
          >
            <div className="rounded-lg bg-surface-2/70 p-3 ring-1 ring-hairline">
              <WhatYouSign proposal={proposal} />
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                <span>
                  {proposal.kind === "PLAN" ? "Plan" : "Order"} ID <span className="num text-foreground">{proposal.short_hash}</span>
                </span>
                <span>Account {displayAccountId(proposal.steps[0]?.canonical.account_id ?? me?.account_id)}</span>
                {proposal.kind === "PLAN" && <span className="text-human">One signature binds all {proposal.steps.length} steps</span>}
              </div>
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center gap-1.5 text-[10.5px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
                <HashIcon className="size-3" /> Payload hash you will sign (SHA-256, RFC 8785)
              </div>
              {startM.isPending ? (
                <Skeleton className="h-14 w-full" />
              ) : start ? (
                <div className="rounded-lg bg-background/60 p-2.5 ring-1 ring-human/25">
                  <p className="num text-[12.5px] leading-relaxed tracking-wide break-all text-human">{chunkHash(start.payload_hash)}</p>
                  <div className="mt-1 flex justify-end">
                    <Hash value={start.payload_hash} display="Copy hash" className="text-[11px] text-muted-foreground" label="payload hash" />
                  </div>
                </div>
              ) : null}
              {start && (
                <Collapsible>
                  <CollapsibleTrigger className="group/payload flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                    <ChevronDown className="size-3 transition-transform group-data-[panel-open]/payload:rotate-180" />
                    What exactly is signed?
                  </CollapsibleTrigger>
                  <CollapsibleContent>
                    <pre className="scrollbar-thin mt-1.5 max-h-44 overflow-auto rounded-md bg-background/60 p-2.5 font-mono text-[11px] leading-relaxed text-muted-foreground ring-1 ring-hairline">
                      {JSON.stringify(start.payload, null, 2)}
                    </pre>
                  </CollapsibleContent>
                </Collapsible>
              )}
            </div>

            <ul className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-3">
              <li className="rounded-md bg-surface-2/60 px-2 py-1.5 ring-1 ring-hairline">
                <span className="font-medium text-foreground">Signed</span> over this exact hash
              </li>
              <li className="rounded-md bg-surface-2/60 px-2 py-1.5 ring-1 ring-hairline">
                <span className="font-medium text-foreground">Single-use</span>, cannot be replayed
              </li>
              <li className="rounded-md bg-surface-2/60 px-2 py-1.5 ring-1 ring-hairline">
                <span className="font-medium text-foreground">Expires in 60 s</span>
              </li>
            </ul>

            {startM.error ? <ApprovalError error={startM.error} onRestart={() => restart()} /> : null}
            {expired && !startM.isPending && (
              <ApprovalError
                error={new ApiError(409, "APPROVAL_EXPIRED", "The 60 s approval window closed before you continued. Nothing was signed.")}
                onRestart={() => restart()}
              />
            )}

            <div className="flex flex-col-reverse gap-2 pt-1 sm:flex-row sm:justify-end">
              <Button variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button
                className="bg-human text-background hover:bg-human/90"
                disabled={!start || expired || startM.isPending}
                onClick={() => setStep("auth")}
              >
                Continue to authenticate
              </Button>
            </div>
          </motion.div>
        ) : (
          <motion.div
            key="auth"
            initial={reduce ? false : { opacity: 0, x: 12 }}
            animate={{ opacity: 1, x: 0 }}
            exit={reduce ? undefined : { opacity: 0, x: -12 }}
            transition={{ duration: 0.18 }}
            className="space-y-3"
          >
            <div className="grid grid-cols-3 gap-1 rounded-lg bg-surface-2/70 p-1 ring-1 ring-hairline" role="tablist" aria-label="Countersign method">
              {METHODS.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  role="tab"
                  aria-selected={method === m.id}
                  disabled={busy}
                  onClick={() => switchMethod(m.id)}
                  className={cn(
                    "flex min-h-9 items-center justify-center gap-1.5 rounded-md px-2 text-xs font-medium transition-colors disabled:opacity-60",
                    method === m.id ? "bg-background text-foreground shadow-sm ring-1 ring-hairline-strong" : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  <m.icon className="size-3.5" />
                  <span className="truncate">{m.id === "totp" ? <><span className="sm:hidden">Code</span><span className="hidden sm:inline">{m.label}</span></> : m.label}</span>
                </button>
              ))}
            </div>

            {method === "webauthn" &&
              (activeDevices.length === 0 ? (
                <RegisterPasskeyInline onDone={() => restart("webauthn")} />
              ) : (
                <div className="space-y-3 rounded-lg bg-surface-2/50 p-4 ring-1 ring-hairline">
                  <p className="text-sm leading-relaxed">
                    Authenticate with this computer&apos;s passkey (Windows Hello or Touch ID, whichever it
                    provides).
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Your device signs the payload hash as the WebAuthn challenge. The private key never leaves the device.
                  </p>
                  <Button
                    size="lg"
                    className="h-11 w-full bg-human text-[15px] font-semibold text-background hover:bg-human/90"
                    disabled={!start?.webauthn || busy || expired}
                    onClick={() => void signPasskey()}
                  >
                    <KeyRound />
                    {signing ? "Waiting for your device..." : completeM.isPending ? "Verifying signature..." : "Authenticate with passkey"}
                  </Button>
                  {registerHere ? (
                    <RegisterPasskeyInline
                      onDone={() => {
                        setRegisterHere(false);
                        restart("webauthn");
                      }}
                    />
                  ) : (
                    <button
                      type="button"
                      onClick={() => setRegisterHere(true)}
                      className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                    >
                      Not this device? Register this device&apos;s passkey
                    </button>
                  )}
                </div>
              ))}

            {method === "pin" &&
              (!me?.has_pin ? (
                <div className="space-y-2 rounded-lg bg-surface-2/50 p-4 ring-1 ring-hairline">
                  <p className="text-sm font-medium">Set a 4-digit app PIN</p>
                  <SetPinInline hasPin={false} onDone={() => restart("pin")} />
                </div>
              ) : (
                <div className="space-y-3 rounded-lg bg-surface-2/50 p-4 ring-1 ring-hairline">
                  <p className="text-sm">Enter your 4-digit app PIN.</p>
                  <PinSlots
                    value={pin}
                    onChange={setPin}
                    length={4}
                    masked
                    autoFocus
                    label="App PIN"
                    disabled={expired}
                    invalid={errorCode(completeM.error) === "PIN_INVALID"}
                    onComplete={(v) => submitPin(v)}
                  />
                  <Button className="w-full bg-human text-background hover:bg-human/90" disabled={pin.length !== 4 || busy || expired} onClick={() => submitPin()}>
                    {completeM.isPending ? "Verifying..." : "Countersign with PIN"}
                  </Button>
                  <p className="text-[11px] text-muted-foreground">PIN approvals are server-attested rather than device-signed. Five wrong attempts lock the PIN.</p>
                </div>
              ))}

            {method === "totp" &&
              (!me?.has_totp ? (
                <div className="space-y-2 rounded-lg bg-surface-2/50 p-4 text-sm ring-1 ring-hairline">
                  <p>No authenticator app is set up for this account yet.</p>
                  <Button variant="outline" size="sm" nativeButton={false} render={<Link href="/security#totp" onClick={onClose} />}>
                    Set up in Security Center
                  </Button>
                </div>
              ) : (
                <div className="space-y-3 rounded-lg bg-surface-2/50 p-4 ring-1 ring-hairline">
                  <p className="text-sm">Enter the current 6-digit code from your authenticator app.</p>
                  <PinSlots
                    value={totp}
                    onChange={setTotp}
                    length={6}
                    autoFocus
                    label="Authenticator code"
                    disabled={expired}
                    invalid={errorCode(completeM.error) === "TOTP_INVALID"}
                    onComplete={(v) => submitTotp(v)}
                  />
                  <Button className="w-full bg-human text-background hover:bg-human/90" disabled={totp.length !== 6 || busy || expired} onClick={() => submitTotp()}>
                    {completeM.isPending ? "Verifying..." : "Countersign with code"}
                  </Button>
                </div>
              ))}

            {start && (
              <p className="num truncate text-[11px] text-subtle" title={start.payload_hash}>
                Signing {start.payload_hash.slice(0, 24)}... for approval {start.approval_id}
              </p>
            )}

            {expired && !busy ? (
              <ApprovalError
                error={new ApiError(409, "APPROVAL_EXPIRED", "The 60 s approval window has closed. Nothing was signed or executed.")}
                onRestart={() => restart()}
              />
            ) : error && !busy ? (
              <ApprovalError error={error} onRestart={() => restart()} />
            ) : null}

            {IS_MOCK && (
              <p className="rounded-md bg-warning/[0.07] px-2.5 py-1.5 text-[11px] text-warning ring-1 ring-warning/25">
                Mock mode: the passkey prompt is simulated. Use the PIN you chose; the authenticator code is {MOCK_TOTP}.
              </p>
            )}

            <div className="flex justify-between pt-1">
              <Button variant="ghost" size="sm" onClick={() => setStep("confirm")} disabled={busy}>
                <ArrowLeft /> Back
              </Button>
              <Button variant="ghost" size="sm" onClick={onClose} disabled={completeM.isPending}>
                Cancel
              </Button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export function CountersignDialog({
  proposal,
  open,
  onOpenChange,
}: {
  proposal: Proposal;
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92dvh] grid-cols-[minmax(0,1fr)] overflow-x-hidden overflow-y-auto p-5 sm:max-w-[540px]">
        {open && <CountersignFlow proposal={proposal} onClose={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  );
}
