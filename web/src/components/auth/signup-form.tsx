"use client";

import { useMutation } from "@tanstack/react-query";
import { ArrowRight, FlaskConical, LoaderCircle } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { EMAIL_RE, PASSWORD_MAX, PASSWORD_MIN, passwordStrength, safeNext, withNext } from "@/lib/auth";
import { isApiError, retryAfterSeconds } from "@/lib/errors";
import { cn } from "@/lib/utils";
import { AuthAlert, AuthCard, Field, authErrorCopy, useAcceptSession, useSignedInRedirect } from "./auth-ui";
import { PasswordInput } from "./password-input";

type FieldName = "display_name" | "email" | "password";

function validate(name: string, email: string, password: string): Partial<Record<FieldName, string>> {
  const errors: Partial<Record<FieldName, string>> = {};
  if (!name.trim()) errors.display_name = "Tell us what to call you.";
  else if (name.trim().length > 64) errors.display_name = "Keep it to 64 characters or fewer.";
  if (!EMAIL_RE.test(email.trim())) errors.email = "Enter a valid email address.";
  if (password.length < PASSWORD_MIN) errors.password = `Use at least ${PASSWORD_MIN} characters.`;
  else if (password.length > PASSWORD_MAX) errors.password = `Use ${PASSWORD_MAX} characters or fewer.`;
  else if (password.trim().toLowerCase() === email.trim().toLowerCase()) errors.password = "Your password must not be your email address.";
  return errors;
}

const METER = ["bg-danger", "bg-warning", "bg-info", "bg-profit"];

function StrengthMeter({ password, email }: { password: string; email: string }) {
  const s = passwordStrength(password, email);
  return (
    <div className="space-y-1.5" aria-live="polite">
      <div className="grid grid-cols-4 gap-1" aria-hidden>
        {[1, 2, 3, 4].map((i) => (
          <span key={i} className={cn("h-1 rounded-full transition-colors", s.score >= i ? METER[s.score - 1] : "bg-hairline-strong")} />
        ))}
      </div>
      <p className="flex justify-between gap-2 text-xs text-muted-foreground">
        <span>{s.hint}</span>
        {s.label && <span className="shrink-0 font-medium text-foreground">{s.label}</span>}
      </p>
    </div>
  );
}

export function SignUpForm() {
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  useSignedInRedirect(next);
  const accept = useAcceptSession();

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const signup = useMutation({
    mutationFn: () => api.signup({ display_name: name.trim(), email: email.trim(), password }),
    onSuccess: accept,
  });

  const errors = submitted ? validate(name, email, password) : {};
  // The API names the offending field in INVALID_INPUT; show it next to that field.
  const serverField = isApiError(signup.error) && signup.error.code === "INVALID_INPUT" ? (signup.error.extra.field as FieldName | undefined) : undefined;
  const fieldError = (f: FieldName) => errors[f] ?? (serverField === f && isApiError(signup.error) ? signup.error.message : null);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    const invalid = Object.keys(validate(name, email, password)) as FieldName[];
    if (invalid.length) {
      const ids: Record<FieldName, string> = { display_name: "signup-name", email: "signup-email", password: "signup-password" };
      document.getElementById(ids[invalid[0]])?.focus();
      return;
    }
    if (signup.isPending) return;
    signup.mutate();
  };

  const err = signup.error;
  const showAlert = err && !serverField;
  const copy = showAlert ? authErrorCopy(err) : null;
  const wait = retryAfterSeconds(err);

  return (
    <AuthCard
      eyebrow="Create your account"
      title="Start with a copilot that cannot trade alone"
      description="Your account comes with its own paper trading portfolio. Next, you will choose the PIN and passkey that countersign your trades."
    >
      <form onSubmit={submit} noValidate className="space-y-4">
        {copy && (
          <AuthAlert title={copy.title}>
            <p>
              {copy.message}
              {wait ? ` Try again in about ${wait} s.` : ""}
            </p>
            {isApiError(err) && err.code === "EMAIL_TAKEN" && (
              <Link href={withNext("/signin", next)} className="mt-1 inline-flex min-h-8 items-center font-medium text-foreground underline underline-offset-4">
                Go to sign in
              </Link>
            )}
          </AuthAlert>
        )}

        <Field id="signup-name" label="Display name" error={fieldError("display_name")}>
          <input
            id="signup-name"
            autoComplete="name"
            autoFocus
            maxLength={64}
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-invalid={fieldError("display_name") ? true : undefined}
            aria-describedby={fieldError("display_name") ? "signup-name-error" : undefined}
            placeholder="How the copilot greets you"
            className="auth-input"
            disabled={signup.isPending}
          />
        </Field>
        <Field id="signup-email" label="Email" error={fieldError("email")}>
          <input
            id="signup-email"
            type="email"
            inputMode="email"
            autoComplete="email"
            autoCapitalize="none"
            spellCheck={false}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={fieldError("email") ? true : undefined}
            aria-describedby={fieldError("email") ? "signup-email-error" : undefined}
            placeholder="you@example.com"
            className="auth-input"
            disabled={signup.isPending}
          />
        </Field>
        <Field id="signup-password" label="Password" error={fieldError("password")} aside={<span className="text-xs text-subtle">{PASSWORD_MIN}+ characters</span>}>
          <PasswordInput
            id="signup-password"
            autoComplete="new-password"
            maxLength={PASSWORD_MAX}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={fieldError("password") ? true : undefined}
            aria-describedby={fieldError("password") ? "signup-password-error" : "signup-password-strength"}
            disabled={signup.isPending}
          />
          {!fieldError("password") && (
            <div id="signup-password-strength" className="pt-1">
              <StrengthMeter password={password} email={email} />
            </div>
          )}
        </Field>

        <div className="flex items-start gap-2.5 rounded-xl bg-info/[0.07] px-3.5 py-3 text-xs leading-relaxed text-muted-foreground ring-1 ring-info/20">
          <FlaskConical className="mt-0.5 size-3.5 shrink-0 text-info" aria-hidden />
          <p>
            <span className="font-medium text-foreground">You start on your own paper trading account.</span> Orders go to a local paper
            broker. Accounts the operator links to the 021 Developer Sandbox trade virtual money there. No real money moves.
          </p>
        </div>

        <Button type="submit" className="h-11 w-full gap-2 bg-human text-[15px] font-semibold text-background hover:bg-human/90" disabled={signup.isPending}>
          {signup.isPending ? (
            <>
              <LoaderCircle className="animate-spin" aria-hidden /> Creating your account...
            </>
          ) : (
            <>
              Create account <ArrowRight aria-hidden />
            </>
          )}
        </Button>

        <p className="text-center text-sm text-muted-foreground">
          Already have an account?{" "}
          <Link
            href={withNext("/signin", next)}
            className="inline-flex min-h-11 items-center font-medium text-foreground underline-offset-4 outline-none hover:underline focus-visible:underline"
          >
            Sign in
          </Link>
        </p>
      </form>
    </AuthCard>
  );
}
