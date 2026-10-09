import type { Metadata } from "next";
import { Suspense } from "react";
import { SignInForm } from "@/components/auth/signin-form";
import { AuthCardSkeleton } from "@/components/auth/auth-skeleton";

export const metadata: Metadata = { title: "Sign in" };

export default function SignInPage() {
  return (
    <Suspense fallback={<AuthCardSkeleton />}>
      <SignInForm />
    </Suspense>
  );
}
