import type { Metadata } from "next";
import { Suspense } from "react";
import { SignUpForm } from "@/components/auth/signup-form";
import { AuthCardSkeleton } from "@/components/auth/auth-skeleton";

export const metadata: Metadata = { title: "Create account" };

export default function SignUpPage() {
  return (
    <Suspense fallback={<AuthCardSkeleton />}>
      <SignUpForm />
    </Suspense>
  );
}
