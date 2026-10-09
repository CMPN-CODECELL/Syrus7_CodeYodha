"use client";

import { useMutation } from "@tanstack/react-query";
import { ArrowRight, Fingerprint, LoaderCircle } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { EMAIL_RE, safeNext, withNext } from "@/lib/auth";
import { isApiError, retryAfterSeconds } from "@/lib/errors";
import { loginWithPasskey } from "@/lib/webauthn";
import { useNow } from "@/hooks/use-now";
import { usePasskeysSupported } from "@/hooks/use-passkeys-supported";
import { AuthAlert, AuthCard, Field, OrDivider, authErrorCopy, useAcceptSession, useSignedInRedirect } from "./auth-ui";
import { PasswordInput } from "./password-input";

function mmss(totalSeconds: number) {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function SignInForm() {
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const { checking } = useSignedInRedirect(next);
  const accept = useAcceptSession();
  const now = useNow(1000);
  const canPasskey = usePasskeysSupported();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [lockedUntil, setLockedUntil] = useState<number | null>(null);

  const onError = (e: unknown) => {
    const wait = retryAfterSeconds(e);
    if (isApiError(e) && (e.code === "LOGIN_LOCKED" || e.code === "RATE_LIMITED") && wait) setLockedUntil(Date.now() + wait * 1000);
    if (isApiError(e) && (e.code === "INVALID_CREDENTIALS" || e.code === "LOGIN_LOCKED")) {
      // A fresh, empty password field: the alert above explains what happened, not a "required" error.
      setPassword("");
      setSubmitted(false);
      // After the inputs re-enable (they are disabled while the request runs).
      if (e.code === "INVALID_CREDENTIALS") window.setTimeout(() => document.getElementById("signin-password")?.focus(), 30);
    }
  };

  const login = useMutation({
    mutationFn: () => api.login({ email: email.trim(), password }),
    onSuccess: accept,
    onError,
  });
  const passkey = useMutation({
    mutationFn: async () => {
      const start = await api.passkeyLoginStart();
      const credential = await loginWithPasskey(start.options);
      return api.passkeyLoginFinish(start.challenge_id, credential);
    },
    onSuccess: accept,
    onError,
  });

  const emailError = submitted && !EMAIL_RE.test(email.trim()) ? "Enter the email you signed up with." : null;
  const passwordError = submitted && !password ? "Enter your password." : null;
  const remaining = lockedUntil && now ? Math.max(0, Math.ceil((lockedUntil - now) / 1000)) : 0;
  const locked = remaining > 0;
  const busy = login.isPending || passkey.isPending;
  const error = login.error ?? passkey.error;
  const lockError = isApiError(error) && error.code === "LOGIN_LOCKED";

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (!EMAIL_RE.test(email.trim())) return document.getElementById("signin-email")?.focus();
    if (!password) return document.getElementById("signin-password")?.focus();
    if (busy || locked) return;
    passkey.reset();
    login.mutate();
  };

  const copy = error && !lockError ? authErrorCopy(error) : null;

  return (
    <AuthCard
      eyebrow="Welcome back"
      title="Sign in to Countersign"
      description="Pick up where you left off. The copilot drafts; only your countersignature can place a trade."
    >
      <div className="space-y-4">
        {lockError && (
          <AuthAlert title="Sign-in is paused for this account" tone="warning">
            {locked ? (
              <p aria-live="polite">
                Too many failed attempts. Try again in <span className="num font-semibold text-foreground">{mmss(remaining)}</span>.
              </p>
            ) : (
              <p>You can try again now.</p>
            )}
          </AuthAlert>
        )}
        {copy && (
          <AuthAlert title={copy.title}>
            <p>
              {copy.message}
              {locked && (
                <>
                  {" "}
                  Retry in <span className="num font-semibold text-foreground">{mmss(remaining)}</span>.
                </>
              )}
            </p>
          </AuthAlert>
        )}

        <form onSubmit={submit} noValidate className="space-y-4">
          <Field id="signin-email" label="Email" error={emailError}>
            <input
              id="signin-email"
              type="email"
              inputMode="email"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              aria-invalid={emailError ? true : undefined}
              aria-describedby={emailError ? "signin-email-error" : undefined}
              placeholder="you@example.com"
              className="auth-input"
              disabled={busy}
            />
          </Field>
          <Field id="signin-password" label="Password" error={passwordError}>
            <PasswordInput
              id="signin-password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-invalid={passwordError ? true : undefined}
              aria-describedby={passwordError ? "signin-password-error" : undefined}
              disabled={busy}
            />
          </Field>
          <Button
            type="submit"
            className="h-11 w-full gap-2 bg-human text-[15px] font-semibold text-background hover:bg-human/90"
            disabled={busy || locked}
          >
            {login.isPending ? (
              <>
                <LoaderCircle className="animate-spin" aria-hidden /> Signing in...
              </>
            ) : locked ? (
              `Try again in ${mmss(remaining)}`
            ) : (
              <>
                Sign in <ArrowRight aria-hidden />
              </>
            )}
          </Button>
        </form>

        <OrDivider />

        <Button
          type="button"
          variant="outline"
          className="h-11 w-full gap-2 text-[15px]"
          disabled={busy || !canPasskey}
          onClick={() => {
            login.reset();
            passkey.mutate();
          }}
        >
          {passkey.isPending ? (
            <>
              <LoaderCircle className="animate-spin" aria-hidden /> Waiting for your device...
            </>
          ) : (
            <>
              <Fingerprint aria-hidden /> Sign in with a passkey
            </>
          )}
        </Button>
        {!canPasskey && <p className="text-center text-xs text-muted-foreground">This browser does not support passkeys.</p>}

        <p className="rounded-xl bg-surface-2/60 px-3.5 py-3 text-xs leading-relaxed text-muted-foreground ring-1 ring-hairline">
          Signing in is not approval. Every trade still needs a fresh countersignature from you, bound to that exact order.
        </p>

        <p className="text-center text-sm text-muted-foreground">
          New to Countersign?{" "}
          <Link
            href={withNext("/signup", next)}
            className="inline-flex min-h-11 items-center font-medium text-foreground underline-offset-4 outline-none hover:underline focus-visible:underline"
          >
            Create an account
          </Link>
        </p>
        {checking && <span className="sr-only">Checking for an existing session</span>}
      </div>
    </AuthCard>
  );
}
