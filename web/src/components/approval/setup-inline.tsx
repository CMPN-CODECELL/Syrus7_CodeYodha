"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, LoaderCircle, ShieldCheck } from "lucide-react";
import { OTPInputContext } from "input-otp";
import { QRCodeSVG } from "qrcode.react";
import { useContext, useState } from "react";
import { toast } from "sonner";
import { PasswordInput } from "@/components/auth/password-input";
import { Button } from "@/components/ui/button";
import { Hash } from "@/components/ui/hash";
import { Input } from "@/components/ui/input";
import { InputOTP, InputOTPGroup } from "@/components/ui/input-otp";
import { ErrorState } from "@/components/ui/states";
import { api, IS_MOCK } from "@/lib/api";
import { activePasskeys, weakPinReason } from "@/lib/auth";
import { isApiError } from "@/lib/errors";
import { MOCK_TOTP } from "@/lib/mock";
import { qk, useMe } from "@/lib/queries";
import type { StepUpMethod } from "@/lib/types";
import { createPasskey, signWithPasskey, suggestDeviceLabel } from "@/lib/webauthn";
import { cn } from "@/lib/utils";
import { usePasskeysSupported } from "@/hooks/use-passkeys-supported";

/** The API refused to add or replace an approval method until the trader re-confirms with an existing one. */
export function needsStepUp(e: unknown): boolean {
  return isApiError(e) && e.code === "STEP_UP_REQUIRED";
}

const STEP_UP_LABEL: Record<StepUpMethod, string> = { webauthn: "Passkey", pin: "App PIN", totp: "Authenticator" };

/** Passkey re-confirmation: step-up/start, the device prompt, then step-up. Proves who you are; it approves nothing. */
async function confirmWithPasskey() {
  const start = await api.stepUpStart();
  const credential = await signWithPasskey(start.options);
  return api.stepUp({ method: "webauthn", challenge_id: start.challenge_id, credential });
}

/**
 * Re-confirm with an approval method you already have (passkey, app PIN or authenticator code) before adding or
 * replacing one. A password or a signed-in session alone cannot do that. On success `onVerified` retries the action.
 */
export function StepUpInline({ error, onVerified, className }: { error?: unknown; onVerified: () => void; className?: string }) {
  const me = useMe().data;
  const fromError = isApiError(error) && Array.isArray(error.extra.methods) ? (error.extra.methods as StepUpMethod[]) : null;
  const methods = (fromError ?? [
    ...(me && activePasskeys(me) > 0 ? (["webauthn"] as const) : []),
    ...(me?.has_pin ? (["pin"] as const) : []),
    ...(me?.has_totp ? (["totp"] as const) : []),
  ]).filter((m): m is StepUpMethod => m in STEP_UP_LABEL);
  const [method, setMethod] = useState<StepUpMethod | null>(null);
  const active = method ?? methods[0] ?? "pin";
  const [value, setValue] = useState("");
  const verify = useMutation({
    mutationFn: (v: string) =>
      active === "webauthn" ? confirmWithPasskey() : api.stepUp(active === "pin" ? { method: "pin", pin: v } : { method: "totp", code: v }),
    onSuccess: () => {
      setValue("");
      onVerified();
    },
    onError: () => setValue(""),
  });
  return (
    <div className={cn("space-y-3 rounded-lg bg-surface-2/60 p-3 ring-1 ring-hairline", className)} role="group" aria-label="Confirm it is you">
      <div className="flex items-start gap-2.5">
        <ShieldCheck className="mt-0.5 size-4 shrink-0 text-human" aria-hidden />
        <div className="space-y-1">
          <p className="text-sm font-medium">Confirm it is you first</p>
          <p className="text-xs text-muted-foreground">
            Adding or replacing a way to countersign needs one you already have. Being signed in is not enough. This confirms who you are;
            it approves no trade.
          </p>
        </div>
      </div>
      {methods.length > 1 && (
        <div className="flex flex-wrap gap-1" role="radiogroup" aria-label="Confirm with">
          {methods.map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={active === m}
              disabled={verify.isPending}
              onClick={() => {
                setMethod(m);
                setValue("");
                verify.reset();
              }}
              className={cn(
                "min-h-8 rounded-md px-2.5 text-xs font-medium ring-1 transition-colors",
                active === m ? "bg-background text-foreground ring-hairline-strong" : "text-muted-foreground ring-transparent hover:text-foreground",
              )}
            >
              {STEP_UP_LABEL[m]}
            </button>
          ))}
        </div>
      )}
      {active === "webauthn" ? (
        <Button className="h-9 bg-human text-background hover:bg-human/90" disabled={verify.isPending} onClick={() => verify.mutate("")}>
          {verify.isPending ? <LoaderCircle className="animate-spin" aria-hidden /> : <KeyRound aria-hidden />}
          {verify.isPending ? "Waiting for your device..." : "Confirm with passkey"}
        </Button>
      ) : (
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">{active === "pin" ? "Your 4-digit app PIN" : "Current 6-digit authenticator code"}</p>
          <PinSlots
            key={active}
            value={value}
            onChange={setValue}
            length={active === "pin" ? 4 : 6}
            masked={active === "pin"}
            autoFocus
            align="start"
            disabled={verify.isPending}
            invalid={!!verify.error}
            label={active === "pin" ? "App PIN to confirm" : "Authenticator code to confirm"}
            onComplete={(v) => verify.mutate(v)}
          />
          {IS_MOCK && active === "totp" && <p className="text-[11px] text-warning">Mock mode code: {MOCK_TOTP}</p>}
        </div>
      )}
      {verify.error && <ErrorState error={verify.error} title="Not confirmed" compact />}
    </div>
  );
}

/** register/start -> startRegistration -> register/finish */
export function useRegisterPasskey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (label: string) => {
      const options = await api.registerStart(label);
      const response = await createPasskey(options);
      return api.registerFinish(response);
    },
    onSuccess: (device) => {
      toast.success("Passkey registered", { description: `${device.label} can now countersign orders.` });
      void qc.invalidateQueries({ queryKey: qk.me });
    },
  });
}

export function RegisterPasskeyInline({ onDone, className }: { onDone?: () => void; className?: string }) {
  const [label, setLabel] = useState(suggestDeviceLabel);
  const reg = useRegisterPasskey();
  const supported = usePasskeysSupported();
  const register = () => reg.mutate(label.trim(), { onSuccess: () => onDone?.() });
  return (
    <div className={cn("space-y-3 rounded-lg bg-surface-2/60 p-3 ring-1 ring-hairline", className)}>
      <div className="flex items-start gap-2.5">
        <KeyRound className="mt-0.5 size-4 shrink-0 text-human" aria-hidden />
        <div className="space-y-1">
          <p className="text-sm font-medium">Register this device&apos;s passkey</p>
          <p className="text-xs text-muted-foreground">
            Your device creates a key pair. The private key never leaves the device; Countersign stores only the public key and
            verifies each signature against it.
          </p>
        </div>
      </div>
      {!supported && (
        <p className="text-xs text-warning">This browser does not support passkeys. Use your app PIN instead.</p>
      )}
      <div className="flex gap-2">
        <Input value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Device label" className="h-9" maxLength={48} />
        <Button
          className="h-9 bg-human text-background hover:bg-human/90"
          disabled={reg.isPending || !label.trim() || !supported}
          onClick={register}
        >
          {reg.isPending ? "Waiting for device..." : "Register passkey"}
        </Button>
      </div>
      {needsStepUp(reg.error) ? (
        <StepUpInline error={reg.error} onVerified={register} className="bg-background/60" />
      ) : (
        reg.error && <ErrorState error={reg.error} title="Passkey was not registered" compact />
      )}
    </div>
  );
}

/** OTP slot that can mask its digit (PINs) and shows a caret on the active slot. */
function Slot({ index, masked, invalid }: { index: number; masked?: boolean; invalid?: boolean }) {
  const ctx = useContext(OTPInputContext);
  const slot = ctx?.slots[index];
  return (
    <div
      data-active={slot?.isActive}
      aria-invalid={invalid || undefined}
      className={cn(
        "num relative flex size-11 items-center justify-center rounded-lg bg-surface-2 text-lg font-semibold ring-1 ring-hairline-strong transition-all",
        slot?.isActive && "z-10 ring-2 ring-human",
        invalid && "ring-danger",
      )}
    >
      {slot?.char ? (masked ? <span className="size-2.5 rounded-full bg-foreground" /> : slot.char) : null}
      {slot?.hasFakeCaret && (
        <span className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <span className="h-5 w-px animate-caret-blink bg-foreground" />
        </span>
      )}
    </div>
  );
}

export function PinSlots({
  value,
  onChange,
  length,
  invalid,
  disabled,
  autoFocus,
  onComplete,
  label,
  masked,
  align = "center",
}: {
  value: string;
  onChange: (v: string) => void;
  length: 4 | 6;
  invalid?: boolean;
  disabled?: boolean;
  autoFocus?: boolean;
  onComplete?: (v: string) => void;
  label: string;
  masked?: boolean;
  /** "start" lines the boxes up under a left-aligned label; dialogs keep them centred. */
  align?: "start" | "center";
}) {
  return (
    <InputOTP
      maxLength={length}
      value={value}
      onChange={onChange}
      onComplete={onComplete}
      disabled={disabled}
      autoFocus={autoFocus}
      inputMode="numeric"
      pattern="^[0-9]+$"
      aria-label={label}
      containerClassName={align === "start" ? "justify-start" : "justify-center"}
    >
      <InputOTPGroup className="gap-2">
        {Array.from({ length }, (_, i) => (
          <Slot key={i} index={i} masked={masked} invalid={invalid} />
        ))}
      </InputOTPGroup>
    </InputOTP>
  );
}

/** Live hints for a new PIN and its confirmation. The server re-checks (WEAK_PIN). */
export function usePinChoice() {
  const [pin, setPin] = useState("");
  const [confirm, setConfirm] = useState("");
  const weak = pin.length === 4 ? weakPinReason(pin) : null;
  const mismatch = confirm.length === 4 && pin !== confirm;
  const ready = pin.length === 4 && pin === confirm && !weak;
  const clear = () => {
    setPin("");
    setConfirm("");
  };
  return { pin, setPin, confirm, setConfirm, weak, mismatch, ready, clear };
}

export function SetPinInline({ hasPin, onDone, className }: { hasPin: boolean; onDone?: () => void; className?: string }) {
  const qc = useQueryClient();
  const [current, setCurrent] = useState("");
  const { pin, setPin, confirm, setConfirm, weak, mismatch, ready, clear } = usePinChoice();
  const set = useMutation({
    mutationFn: () => api.setPin(pin, hasPin ? current : undefined),
    onSuccess: () => {
      toast.success(hasPin ? "PIN changed" : "App PIN set", { description: "Use it as a fallback to countersign and to disengage the kill switch." });
      clear();
      setCurrent("");
      void qc.invalidateQueries({ queryKey: qk.me });
      onDone?.();
    },
  });
  return (
    <div className={cn("space-y-3", className)}>
      {hasPin && (
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">Current PIN</p>
          <PinSlots value={current} onChange={setCurrent} length={4} label="Current PIN" masked />
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">New 4-digit PIN</p>
          <PinSlots value={pin} onChange={setPin} length={4} label="New PIN" invalid={!!weak} masked />
        </div>
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">Confirm PIN</p>
          <PinSlots value={confirm} onChange={setConfirm} length={4} label="Confirm PIN" invalid={mismatch} masked />
        </div>
      </div>
      {weak ? (
        <p className="text-xs text-warning">{weak}</p>
      ) : mismatch ? (
        <p className="text-xs text-danger">The two PINs do not match.</p>
      ) : null}
      <Button disabled={set.isPending || !ready || (hasPin && current.length !== 4)} onClick={() => set.mutate()}>
        {set.isPending ? "Saving..." : hasPin ? "Change PIN" : "Set PIN"}
      </Button>
      {set.error && (
        <ErrorState error={set.error} title={isApiError(set.error) && set.error.code === "WEAK_PIN" ? "That PIN is too easy to guess" : "PIN was not saved"} compact />
      )}
    </div>
  );
}

/**
 * Forgotten or locked PIN: prove it is you with the account password plus, when you have one, a second method that
 * is not the PIN (a code from your authenticator app, or your passkey).
 */
export function PinResetInline({
  hasTotp,
  hasPasskey = false,
  onDone,
  className,
}: {
  hasTotp: boolean;
  hasPasskey?: boolean;
  onDone?: () => void;
  className?: string;
}) {
  const qc = useQueryClient();
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [second, setSecond] = useState<"totp" | "passkey">(hasTotp ? "totp" : "passkey");
  const useCode = hasTotp && second === "totp";
  const usePasskey = hasPasskey && !useCode;
  const { pin, setPin, confirm, setConfirm, weak, mismatch, ready, clear } = usePinChoice();
  const reset = useMutation({
    mutationFn: async () => {
      if (usePasskey) await confirmWithPasskey();
      return api.pinReset({ password, new_pin: pin, ...(useCode ? { totp } : {}) });
    },
    onSuccess: () => {
      toast.success("PIN reset", { description: "Your new PIN works right away and any PIN lockout is cleared." });
      setPassword("");
      setTotp("");
      clear();
      void qc.invalidateQueries({ queryKey: qk.me });
      onDone?.();
    },
    onError: (e) => {
      if (isApiError(e) && e.code === "INVALID_CREDENTIALS") setPassword("");
      if (isApiError(e) && (e.code === "TOTP_INVALID" || e.code === "TOTP_REQUIRED")) setTotp("");
    },
  });
  const err = reset.error;
  const title = !isApiError(err)
    ? "PIN was not reset"
    : err.code === "INVALID_CREDENTIALS"
      ? "Password not accepted"
      : err.code === "WEAK_PIN"
        ? "That PIN is too easy to guess"
        : err.code === "TOTP_REQUIRED" || err.code === "TOTP_INVALID"
          ? "Authenticator code needed"
          : err.code === "PASSKEY_REQUIRED"
            ? "Passkey confirmation needed"
          : err.code === "RATE_LIMITED"
            ? "Too many attempts"
            : "PIN was not reset";
  return (
    <form
      className={cn("space-y-3", className)}
      onSubmit={(e) => {
        e.preventDefault();
        if (ready && password && (!useCode || totp.length === 6) && !reset.isPending) reset.mutate();
      }}
    >
      <div className="space-y-1.5">
        <label htmlFor="pin-reset-password" className="text-xs text-muted-foreground">
          Account password
        </label>
        <PasswordInput
          id="pin-reset-password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          inputClassName="h-10 text-sm"
        />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">New 4-digit PIN</p>
          <PinSlots value={pin} onChange={setPin} length={4} label="New PIN" invalid={!!weak} masked />
        </div>
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">Confirm PIN</p>
          <PinSlots value={confirm} onChange={setConfirm} length={4} label="Confirm new PIN" invalid={mismatch} masked />
        </div>
      </div>
      {weak ? (
        <p className="text-xs text-warning">{weak}</p>
      ) : mismatch ? (
        <p className="text-xs text-danger">The two PINs do not match.</p>
      ) : null}
      {hasTotp && hasPasskey && (
        <div className="flex flex-wrap items-center gap-1 text-xs" role="radiogroup" aria-label="Second check">
          <span className="mr-1 text-muted-foreground">Also confirm with</span>
          {(["totp", "passkey"] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={second === m}
              onClick={() => setSecond(m)}
              className={cn(
                "min-h-8 rounded-md px-2.5 font-medium ring-1 transition-colors",
                second === m ? "bg-background text-foreground ring-hairline-strong" : "text-muted-foreground ring-transparent hover:text-foreground",
              )}
            >
              {m === "totp" ? "Authenticator code" : "Passkey"}
            </button>
          ))}
        </div>
      )}
      {useCode && (
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">Authenticator code</p>
          <PinSlots value={totp} onChange={setTotp} length={6} label="Authenticator code" />
          {IS_MOCK && <p className="text-[11px] text-warning">Mock mode code: {MOCK_TOTP}</p>}
        </div>
      )}
      {usePasskey && <p className="text-xs text-muted-foreground">Your device will ask you to confirm with your passkey before the PIN changes.</p>}
      <Button type="submit" disabled={reset.isPending || !ready || !password || (useCode && totp.length !== 6)}>
        {reset.isPending ? "Verifying..." : usePasskey ? "Confirm with passkey and reset PIN" : "Reset PIN"}
      </Button>
      {err && <ErrorState error={err} title={title} compact />}
    </form>
  );
}

/** Link an authenticator app: generate a secret, show the QR, confirm with a current code. */
export function TotpSetupInline({ onDone, className, startClassName }: { onDone?: () => void; className?: string; startClassName?: string }) {
  const qc = useQueryClient();
  const [code, setCode] = useState("");
  const setup = useMutation({ mutationFn: api.totpSetup });
  const grouped = setup.data?.secret.match(/.{1,4}/g)?.join(" ") ?? "";
  const verify = useMutation({
    mutationFn: (c: string) => api.totpVerify(c),
    onSuccess: () => {
      toast.success("Authenticator app enabled", { description: "You can now countersign with a 6-digit code." });
      setCode("");
      setup.reset();
      void qc.invalidateQueries({ queryKey: qk.me });
      onDone?.();
    },
    onError: () => setCode(""),
  });
  return (
    <div className={cn("space-y-3", className)}>
      {!setup.data ? (
        <Button onClick={() => setup.mutate()} disabled={setup.isPending} className={startClassName}>
          {setup.isPending ? "Generating secret..." : "Set up authenticator"}
        </Button>
      ) : (
        <div className="grid gap-4 sm:grid-cols-[auto_1fr] sm:items-start">
          <div className="w-fit rounded-lg bg-white p-2.5">
            <QRCodeSVG value={setup.data.otpauth_uri} size={148} level="M" marginSize={0} title="Scan with your authenticator app" />
          </div>
          <div className="min-w-0 space-y-2.5">
            <p className="text-xs text-muted-foreground">Scan the code, or enter the secret manually:</p>
            <Hash value={setup.data.secret} display={grouped} wrap className="rounded-md bg-surface-2 px-2 py-1 text-[13px] tracking-wider" label="TOTP secret" />
            <p className="text-xs text-muted-foreground">Then enter the current 6-digit code to confirm:</p>
            <PinSlots value={code} onChange={setCode} length={6} label="Authenticator code" align="start" invalid={!!verify.error} onComplete={(v) => verify.mutate(v)} />
            {IS_MOCK && <p className="text-[11px] text-warning">Mock mode code: {MOCK_TOTP}</p>}
          </div>
        </div>
      )}
      {needsStepUp(setup.error) ? (
        <StepUpInline error={setup.error} onVerified={() => setup.mutate()} />
      ) : (
        (setup.error || verify.error) && <ErrorState error={setup.error ?? verify.error} title={verify.error ? "Code not accepted" : "Setup failed"} compact />
      )}
    </div>
  );
}
