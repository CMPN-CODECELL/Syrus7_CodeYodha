"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { exploreHref, useLandingSession } from "./hooks";
import { arrowNudge, btnPrimary } from "./styles";

/** The main call to action. Signed out it asks for sign-in first; signed in it opens the copilot. */
export function ExploreButton({ className, label = "Explore the Countersign Copilot" }: { className?: string; label?: string }) {
  const { signedIn, pending } = useLandingSession();
  return (
    <Link href={exploreHref(signedIn, pending)} className={cn(btnPrimary, className)}>
      {label}
      <ArrowRight className={arrowNudge} aria-hidden />
    </Link>
  );
}

/** A link into a signed-in page; signed-out visitors are sent through sign-in and returned to it. */
export function AppLink({
  to,
  className,
  children,
}: {
  to: string;
  className?: string;
  children: ReactNode;
}) {
  const { signedIn, pending } = useLandingSession();
  return (
    <Link href={signedIn || pending ? to : `/signin?next=${to}`} className={className}>
      {children}
    </Link>
  );
}

/** Rendered only for signed-out visitors (for example "Create an account"), and only once that is known. */
export function SignedOutOnly({ children }: { children: ReactNode }) {
  const { signedIn, pending } = useLandingSession();
  return signedIn || pending ? null : <>{children}</>;
}
