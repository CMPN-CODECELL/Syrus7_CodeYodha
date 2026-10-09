"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import {
  ArrowRight,
  Check,
  Eye,
  EyeOff,
  Fingerprint,
  KeyRound,
  LoaderCircle,
  LockKeyhole,
  ScanFace,
  ShieldCheck,
  Smartphone,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { needsStepUp, PinSlots, StepUpInline, TotpSetupInline, usePinChoice, useRegisterPasskey } from "@/components/approval/setup-inline";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/states";
import { api } from "@/lib/api";
import { accountId, activePasskeys, DEFAULT_AFTER_SIGNIN, onboardingComplete, safeNext } from "@/lib/auth";
import { errorMessage, isApiError } from "@/lib/errors";
import { qk, useMe } from "@/lib/queries";
import { useChatStore } from "@/lib/store";
import type { Me } from "@/lib/types";
import { cn } from "@/lib/utils";
import { suggestDeviceLabel } from "@/lib/webauthn";
import { usePasskeysSupported } from "@/hooks/use-passkeys-supported";
import { AuthAlert, AuthCard } from "./auth-ui";

type StepId = "pin" | "passkey" | "totp" | "done";

// The step in progress survives a reload of this tab, so a refresh after saving the PIN resumes the guided setup.
const STEP_KEY = "cs_onboarding_step";

function savedStep(): StepId | null {
  try {
    const v = sessionStorage.getItem(STEP_KEY);
    return v === "passkey" || v === "totp" || v === "done" ? v : null;
  } catch {
    return null;
  }
}

function saveStep(step: StepId | null) {
  try {
    if (step) sessionStorage.setItem(STEP_KEY, step);
    else sessionStorage.removeItem(STEP_KEY);
  } catch {
    // storage unavailable: a reload simply shows the summary
  }
}

/** "Enter the Copilot" must open the Copilot, even when sign-in started from the landing page ("/"). */
function appTarget(next: string): string {
  return next === "/" ? DEFAULT_AFTER_SIGNIN : next;
}

const STEPS: { id: StepId; label: string; tag: string }[] = [
  { id: "pin", label: "App PIN", tag: "Required" },
  { id: "passkey", label: "Passkey", tag: "Recommended" },
  { id: "totp", label: "Authenticator", tag: "Optional" },
  { id: "done", label: "Done", tag: "" },
];

function Stepper({ current }: { current: StepId }) {
  const idx = STEPS.findIndex((s) => s.id === current);
  return (
    <ol className="flex items-center gap-1.5" aria-label="Setup progress">
      {STEPS.map((s, i) => {
        const done = i < idx;
        const active = i === idx;
        return (
          <li key={s.id} className={cn("flex items-center gap-1.5", i < STEPS.length - 1 && "min-w-0 flex-1")} aria-current={active ? "step" : undefined}>
            <span
              className={cn(
                "grid size-6 shrink-0 place-items-center rounded-full text-[11px] font-semibold ring-1 transition-colors",
                done && "bg-human text-background ring-human",
                active && "bg-human/15 text-human ring-human/60",
                !done && !active && "text-subtle ring-hairline-strong",
              )}
            >
              {done ? <Check className="size-3.5" aria-hidden /> : i + 1}
            </span>
            <span className={cn("sr-only shrink-0 text-xs sm:not-sr-only", active ? "font-medium text-foreground" : "text-muted-foreground")}>{s.label}</span>
            <span className="sr-only">{done ? "(done)" : active ? "(current step)" : ""}</span>
            {i < STEPS.length - 1 && <span className={cn("h-px min-w-3 flex-1", done ? "bg-human/60" : "bg-hairline-strong")} aria-hidden />}
          </li>
        );
      })}
    </ol>
  );
}

function StepFrame({ step, title, description, children }: { step: StepId; title: string; description: ReactNode; children: ReactNode }) {
  const reduce = useReducedMotion();
  const meta = STEPS.find((s) => s.id === step)!;
  const n = STEPS.findIndex((s) => s.id === step) + 1;
  return (
    <motion.section
      key={step}
      initial={reduce ? false : { opacity: 0, x: 16 }}
      animate={{ opacity: 1, x: 0 }}
      exit={reduce ? undefined : { opacity: 0, x: -16 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      aria-labelledby={`step-${step}-title`}
      className="space-y-4"
    >
      <div>
        <p className="text-[11px] font-semibold tracking-[0.12em] text-human uppercase">
          Step {n} of {STEPS.length}
          {meta.tag ? ` · ${meta.tag}` : ""}
        </p>
        <h2 id={`step-${step}-title`} className="mt-1.5 font-serif text-[28px] leading-tight tracking-tight sm:text-[32px]">
          {title}
        </h2>
        <div className="mt-2 text-sm leading-relaxed text-muted-foreground">{description}</div>
      </div>
      {children}
    </motion.section>
  );
}

const primaryBtn = "h-11 gap-2 bg-human px-4 text-[15px] font-semibold text-background hover:bg-human/90";

function PinStep({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [show, setShow] = useState(false);
  const { pin, setPin, confirm, setConfirm, weak, mismatch, ready } = usePinChoice();
  const save = useMutation({
    mutationFn: () => api.setPin(pin),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: qk.me });
      onDone();
    },
  });
  const err = save.error;
  return (
    <StepFrame
      step="pin"
      title="Choose your app PIN"
      description="Your 4-digit PIN is a fallback way to countersign a trade, and it is what switches the kill switch back off. Choose one only you know."
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready && !save.isPending) save.mutate();
        }}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <p className="text-[13px] font-medium">New PIN</p>
            <PinSlots value={pin} onChange={setPin} length={4} label="New 4-digit PIN" invalid={!!weak} masked={!show} autoFocus align="start" />
          </div>
          <div className="space-y-2">
            <p className="text-[13px] font-medium">Confirm PIN</p>
            <PinSlots value={confirm} onChange={setConfirm} length={4} label="Confirm your PIN" invalid={mismatch} masked={!show} align="start" />
          </div>
        </div>
        <div className="flex min-h-5 items-center justify-between gap-3">
          <p className={cn("text-xs", weak ? "text-warning" : mismatch ? "text-danger" : "text-muted-foreground")} aria-live="polite">
            {weak ?? (mismatch ? "The two PINs do not match." : "Avoid 1234, 1111 or anything printed on your cards.")}
          </p>
          <button
            type="button"
            onClick={() => setShow((v) => !v)}
            aria-pressed={show}
            className="inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-md px-2 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            {show ? <EyeOff className="size-3.5" aria-hidden /> : <Eye className="size-3.5" aria-hidden />}
            {show ? "Hide digits" : "Show digits"}
          </button>
        </div>
        {err && (
          <ErrorState
            error={err}
            title={isApiError(err) && err.code === "WEAK_PIN" ? "That PIN is too easy to guess" : "Your PIN was not saved"}
            compact
          />
        )}
        <Button type="submit" className={cn(primaryBtn, "w-full sm:w-auto")} disabled={!ready || save.isPending}>
          {save.isPending ? (
            <>
              <LoaderCircle className="animate-spin" aria-hidden /> Saving...
            </>
          ) : (
            <>
              Save PIN and continue <ArrowRight aria-hidden />
            </>
          )}
        </Button>
      </form>
    </StepFrame>
  );
}

function PasskeyStep({ me, onDone }: { me: Me; onDone: () => void }) {
  const supported = usePasskeysSupported();
  const [label, setLabel] = useState(suggestDeviceLabel);
  const reg = useRegisterPasskey();
  const existing = activePasskeys(me);
  const register = () => reg.mutate(label.trim(), { onSuccess: () => onDone() });
  return (
    <StepFrame
      step="passkey"
      title="Add a passkey on this device"
      description={
        <>
          Countersign with Windows Hello or Touch ID, whichever this computer offers. Your fingerprint or face never leaves the
          device and Countersign never sees it: the device only sends a signature over the exact order you approve. A passkey is
          also the way back in if password sign-in is ever paused.
        </>
      }
    >
      <div className="flex items-center gap-2 text-muted-foreground" aria-hidden>
        {[Fingerprint, ScanFace, KeyRound].map((Icon, i) => (
          <span key={i} className="grid size-10 place-items-center rounded-xl bg-human/10 text-human ring-1 ring-human/25">
            <Icon className="size-5" />
          </span>
        ))}
        <span className="ml-1 text-xs">Device-signed, the strongest way to countersign</span>
      </div>
      {existing > 0 ? (
        <>
          <AuthAlert title={`This account already has ${existing} passkey${existing === 1 ? "" : "s"}`} tone="warning">
            You can add more devices later in the Security Center.
          </AuthAlert>
          <Button className={cn(primaryBtn, "w-full sm:w-auto")} onClick={() => onDone()}>
            Continue <ArrowRight aria-hidden />
          </Button>
        </>
      ) : (
        <>
          {!supported && (
            <AuthAlert title="This browser cannot create passkeys" tone="warning">
              Skip this step and use your PIN for now. You can add a passkey later from a supported browser.
            </AuthAlert>
          )}
          <div className="space-y-1.5">
            <label htmlFor="passkey-label" className="text-[13px] font-medium">
              Device name
            </label>
            <input
              id="passkey-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              maxLength={48}
              className="auth-input"
              disabled={reg.isPending || !supported}
            />
            <p className="text-xs text-muted-foreground">Helps you recognise this device in the Security Center.</p>
          </div>
          {needsStepUp(reg.error) ? (
            <StepUpInline error={reg.error} onVerified={register} />
          ) : (
            reg.error && <ErrorState error={reg.error} title="The passkey was not registered" compact />
          )}
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:items-center">
            <Button variant="ghost" className="h-11 px-4 text-sm" onClick={() => onDone()} disabled={reg.isPending}>
              Skip for now
            </Button>
            <Button
              className={cn(primaryBtn, "sm:ml-auto")}
              disabled={!supported || !label.trim() || reg.isPending}
              onClick={register}
            >
              {reg.isPending ? (
                <>
                  <LoaderCircle className="animate-spin" aria-hidden /> Waiting for your device...
                </>
              ) : (
                <>
                  <Fingerprint aria-hidden /> Register passkey
                </>
              )}
            </Button>
          </div>
        </>
      )}
    </StepFrame>
  );
}

function TotpStep({ me, onDone }: { me: Me; onDone: () => void }) {
  return (
    <StepFrame
      step="totp"
      title="Link an authenticator app"
      description="A second fallback for when your passkey is not at hand. Any TOTP app works. Codes change every 30 seconds and the server checks them."
    >
      {me.has_totp ? (
        <>
          <AuthAlert title="An authenticator app is already linked" tone="warning">
            Nothing to do here.
          </AuthAlert>
          <Button className={cn(primaryBtn, "w-full sm:w-auto")} onClick={() => onDone()}>
            Continue <ArrowRight aria-hidden />
          </Button>
        </>
      ) : (
        <>
          <TotpSetupInline onDone={() => onDone()} startClassName={primaryBtn} />
          <div className="flex">
            <Button variant="ghost" className="h-11 px-4 text-sm" onClick={() => onDone()}>
              Skip, I will use my passkey and PIN
            </Button>
          </div>
        </>
      )}
    </StepFrame>
  );
}

function SummaryRow({ icon: Icon, label, value, good }: { icon: LucideIcon; label: string; value: string; good: boolean }) {
  return (
    <li className="flex items-center gap-3 py-2.5">
      <span className={cn("grid size-8 shrink-0 place-items-center rounded-lg ring-1", good ? "bg-human/10 text-human ring-human/25" : "bg-surface-2 text-subtle ring-hairline")}>
        <Icon className="size-4" aria-hidden />
      </span>
      <span className="min-w-0 flex-1 text-sm">{label}</span>
      <span className={cn("shrink-0 text-xs", good ? "font-medium text-human" : "text-muted-foreground")}>{value}</span>
    </li>
  );
}

function Summary({ me }: { me: Me }) {
  const passkeys = activePasskeys(me);
  return (
    <ul className="divide-y divide-hairline rounded-xl bg-surface-2/50 px-3 ring-1 ring-hairline">
      <SummaryRow icon={LockKeyhole} label="App PIN" value={me.has_pin ? "Set" : "Not set"} good={me.has_pin} />
      <SummaryRow
        icon={Fingerprint}
        label="Passkey"
        value={passkeys ? `${passkeys} registered` : "None yet"}
        good={passkeys > 0}
      />
      <SummaryRow icon={Smartphone} label="Authenticator app" value={me.has_totp ? "Linked" : "Not linked"} good={me.has_totp} />
    </ul>
  );
}

const NOT_APPROVAL =
  "Signing in is not approval. The copilot can only draft. Every trade waits for a fresh countersignature from you, bound to that exact order and valid for 60 seconds.";

function DoneStep({ me, next }: { me: Me; next: string }) {
  const router = useRouter();
  const enter = () => {
    saveStep(null);
    router.push(appTarget(next));
  };
  return (
    <StepFrame step="done" title="You are ready to countersign" description="Here is how you can approve trades from now on.">
      <Summary me={me} />
      <div className="flex items-start gap-2.5 rounded-xl bg-human/[0.07] px-3.5 py-3 text-xs leading-relaxed text-muted-foreground ring-1 ring-human/20">
        <ShieldCheck className="mt-0.5 size-4 shrink-0 text-human" aria-hidden />
        <p>{NOT_APPROVAL}</p>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Button className={cn(primaryBtn, "w-full sm:w-auto")} onClick={enter} autoFocus>
          Enter the Copilot <ArrowRight aria-hidden />
        </Button>
        <Link
          href="/security"
          className="inline-flex min-h-11 items-center justify-center rounded-lg px-3 text-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring sm:ml-auto"
        >
          Review security settings
        </Link>
      </div>
    </StepFrame>
  );
}

function SignedInAs({ me }: { me: Me }) {
  const [busy, setBusy] = useState(false);
  return (
    <p className="mb-5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
      Signed in as <span className="font-medium text-foreground">{me.user.display_name}</span>
      {me.email && <span className="truncate">({me.email})</span>}
      <span aria-hidden>·</span>
      <span className="num">{accountId(me)}</span>
      <button
        type="button"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api.logout();
          } catch {
            // the session may already be gone; leaving is still correct
          }
          useChatStore.getState().reset();
          saveStep(null);
          window.location.replace("/signin");
        }}
        className="ml-auto inline-flex min-h-8 items-center rounded px-1 font-medium text-foreground underline-offset-4 hover:underline focus-visible:underline disabled:opacity-60"
      >
        Not you? Sign out
      </button>
    </p>
  );
}

export function OnboardingFlow() {
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const router = useRouter();
  const me = useMe();
  // Decided once, from the first /api/me: a finished account sees "already set up" instead of the steps.
  const [start, setStart] = useState<"steps" | "already" | null>(null);
  const [step, setStep] = useState<StepId>("pin");

  const unauthenticated = isApiError(me.error) && (me.error.status === 401 || me.error.status === 403);
  useEffect(() => {
    if (unauthenticated) router.replace(`/signin?next=${encodeURIComponent(next)}`);
  }, [unauthenticated, next, router]);

  if (me.data && !me.isError && start === null) {
    const complete = onboardingComplete(me.data);
    const resume = complete ? savedStep() : null;
    setStart(complete && !resume ? "already" : "steps");
    setStep(resume ?? (me.data.has_pin ? "passkey" : "pin"));
  }
  const go = (s: StepId) => {
    saveStep(s);
    setStep(s);
  };

  if (me.isPending || unauthenticated || (me.data && start === null)) {
    return (
      <AuthCard title="Setting things up" className="max-w-[560px]">
        <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <LoaderCircle className="size-4 animate-spin" aria-hidden /> {unauthenticated ? "Taking you to sign in..." : "Loading your account..."}
        </p>
      </AuthCard>
    );
  }

  if (me.error || !me.data) {
    return (
      <AuthCard title="We could not load your account" className="max-w-[560px]">
        <AuthAlert title="Countersign did not respond">{errorMessage(me.error)}</AuthAlert>
        <Button className="mt-4 h-11" variant="outline" onClick={() => void me.refetch()}>
          Try again
        </Button>
      </AuthCard>
    );
  }

  const data = me.data;

  if (start === "already") {
    return (
      <AuthCard eyebrow="Account setup" title="Your account is already set up" className="max-w-[560px]">
        <SignedInAs me={data} />
        <div className="space-y-4">
          <Summary me={data} />
          <p className="text-xs leading-relaxed text-muted-foreground">{NOT_APPROVAL}</p>
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
            <Button className={cn(primaryBtn, "w-full sm:w-auto")} onClick={() => router.push(appTarget(next))} autoFocus>
              Continue to the app <ArrowRight aria-hidden />
            </Button>
            {activePasskeys(data) === 0 && (
              <Button
                variant="outline"
                className="h-11 px-4 text-sm"
                onClick={() => {
                  go("passkey");
                  setStart("steps");
                }}
              >
                <Fingerprint aria-hidden /> Add a passkey now
              </Button>
            )}
          </div>
          <Link
            href="/security"
            className="inline-flex min-h-11 items-center rounded-lg text-sm text-muted-foreground underline-offset-4 outline-none hover:text-foreground hover:underline focus-visible:underline"
          >
            Manage sign-in and countersign methods in the Security Center
          </Link>
        </div>
      </AuthCard>
    );
  }

  return (
    <div className="w-full max-w-[560px] rounded-2xl border border-hairline-strong bg-surface-1/85 p-5 shadow-2xl shadow-black/10 backdrop-blur-xl sm:p-8 dark:shadow-black/40">
      <SignedInAs me={data} />
      <Stepper current={step} />
      <div className="mt-6">
        <AnimatePresence mode="wait" initial={false}>
          {step === "pin" && <PinStep key="pin" onDone={() => go("passkey")} />}
          {step === "passkey" && <PasskeyStep key="passkey" me={data} onDone={() => go("totp")} />}
          {step === "totp" && <TotpStep key="totp" me={data} onDone={() => go("done")} />}
          {step === "done" && <DoneStep key="done" me={data} next={next} />}
        </AnimatePresence>
      </div>
    </div>
  );
}
