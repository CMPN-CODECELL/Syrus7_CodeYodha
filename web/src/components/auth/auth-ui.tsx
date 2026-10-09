"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, type ReactNode } from "react";
import { afterSignIn } from "@/lib/auth";
import { isApiError } from "@/lib/errors";
import { qk, useOptionalMe } from "@/lib/queries";
import { useChatStore } from "@/lib/store";
import type { Me } from "@/lib/types";
import { cn } from "@/lib/utils";
import { PasskeyError } from "@/lib/webauthn";

export function AuthCard({
  eyebrow,
  title,
  description,
  children,
  className,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "w-full max-w-[440px] rounded-2xl border border-hairline-strong bg-surface-1/85 p-5 shadow-2xl shadow-black/10 backdrop-blur-xl sm:p-8 dark:shadow-black/40",
        className,
      )}
    >
      {eyebrow && <div className="mb-3 text-[11px] font-semibold tracking-[0.12em] text-human uppercase">{eyebrow}</div>}
      <h1 className="font-serif text-[34px] leading-[1.05] tracking-tight text-balance sm:text-[40px]">{title}</h1>
      {description && <p className="mt-2.5 text-sm leading-relaxed text-muted-foreground">{description}</p>}
      <div className="mt-6">{children}</div>
    </div>
  );
}

export function Field({
  id,
  label,
  hint,
  error,
  aside,
  children,
}: {
  id: string;
  label: string;
  hint?: ReactNode;
  error?: string | null;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={id} className="text-[13px] font-medium">
          {label}
        </label>
        {aside}
      </div>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-xs text-danger">
          {error}
        </p>
      ) : hint ? (
        <div id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </div>
      ) : null}
    </div>
  );
}

export function AuthAlert({ title, children, tone = "danger" }: { title: string; children?: ReactNode; tone?: "danger" | "warning" }) {
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2.5 rounded-xl p-3 text-sm ring-1",
        tone === "danger" ? "bg-danger/[0.07] ring-danger/30" : "bg-warning/[0.08] ring-warning/30",
      )}
    >
      <CircleAlert className={cn("mt-0.5 size-4 shrink-0", tone === "danger" ? "text-danger" : "text-warning")} aria-hidden />
      <div className="min-w-0 space-y-0.5">
        <p className="font-medium text-foreground">{title}</p>
        {children && <div className="text-[13px] text-muted-foreground">{children}</div>}
      </div>
    </div>
  );
}

export function OrDivider() {
  return (
    <div className="flex items-center gap-3 text-[11px] tracking-[0.12em] text-subtle uppercase" role="separator">
      <span className="h-px flex-1 bg-hairline-strong" />
      or
      <span className="h-px flex-1 bg-hairline-strong" />
    </div>
  );
}

/** Title and body for an auth error. Codes come from the API contract; anything else is shown as sent. */
export function authErrorCopy(e: unknown): { title: string; message: string } {
  if (e instanceof PasskeyError) return { title: "Passkey sign-in did not complete", message: e.message };
  if (!isApiError(e)) {
    return { title: "Something blocked the request", message: e instanceof Error && e.message ? e.message : "Try again in a moment." };
  }
  switch (e.code) {
    case "INVALID_CREDENTIALS":
      return { title: "Email or password is incorrect", message: "Check both and try again. Passwords are case-sensitive." };
    case "RATE_LIMITED":
      return { title: "Too many sign-in attempts", message: "Please wait a moment, then try again." };
    case "ORIGIN_REJECTED":
      return { title: "Request blocked for safety", message: e.message || "Open Countersign at http://localhost:3000 and try again." };
    case "EMAIL_TAKEN":
      return { title: "An account already uses this email", message: "Sign in instead, or use a different email." };
    case "PASSKEY_NOT_RECOGNIZED":
      return { title: "Passkey not recognized", message: e.message || "This passkey is not linked to a Countersign account. Sign in with your email and password." };
    case "SIGNATURE_INVALID":
      return { title: "Passkey signature could not be verified", message: "You are not signed in. Try again, or sign in with your email and password." };
    case "CHALLENGE_EXPIRED":
      return { title: "The passkey request expired", message: "It took too long to confirm on your device. Try again." };
    case "NETWORK_ERROR":
    case "BACKEND_UNAVAILABLE":
    case "BACKEND_ERROR":
      return { title: "Cannot reach Countersign", message: e.message };
    default:
      if (e.status === 404 || e.status === 405) {
        return {
          title: "Account sign-in is not available yet",
          message: "This server does not have the sign-in endpoints yet. Restart the backend with the latest code and try again.",
        };
      }
      return { title: "That did not work", message: e.message };
  }
}

/**
 * Signed-in visitors skip the auth pages. /api/me is asked only when a session cookie exists (no 401 for a
 * signed-out visitor), and a stale cache entry that has since failed is not a session: only a successful,
 * error-free /api/me counts.
 */
export function useSignedInRedirect(next: string) {
  const router = useRouter();
  const { me: signedIn, pending } = useOptionalMe();
  const done = useRef(false);
  useEffect(() => {
    if (!signedIn || done.current) return;
    done.current = true;
    router.replace(afterSignIn(signedIn, next));
  }, [signedIn, next, router]);
  return { checking: pending, signedIn: !!signedIn };
}

/** After sign-in or sign-up: drop every other user's cached data and chat, keep the new session. */
export function useAcceptSession() {
  const qc = useQueryClient();
  return (me: Me) => {
    void qc.cancelQueries();
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== qk.me[0] });
    qc.setQueryData(qk.me, me);
    // A conversation left in this tab belongs to whoever was signed in before (the API answers 404 for it).
    useChatStore.getState().reset();
  };
}
