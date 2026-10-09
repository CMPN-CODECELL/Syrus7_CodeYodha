import type { Metadata } from "next";
import { Suspense } from "react";
import { OnboardingFlow } from "@/components/auth/onboarding-flow";
import { AuthCardSkeleton } from "@/components/auth/auth-skeleton";

export const metadata: Metadata = { title: "Set up your countersignature" };

/** Top-anchored (unlike sign-in) so the stepper stays put while steps of different heights come and go. */
export default function OnboardingPage() {
  return (
    <div className="flex w-full justify-center sm:mt-[6vh] sm:self-start">
      <Suspense fallback={<AuthCardSkeleton wide />}>
        <OnboardingFlow />
      </Suspense>
    </div>
  );
}
