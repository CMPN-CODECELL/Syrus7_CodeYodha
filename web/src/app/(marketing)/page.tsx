import type { Metadata } from "next";
import { AttackLabSection } from "@/components/landing/attack-lab-section";
import { BiometricsBand } from "@/components/landing/biometrics-band";
import { Faq } from "@/components/landing/faq";
import { FinalCta } from "@/components/landing/final-cta";
import { Hero } from "@/components/landing/hero";
import { HowItWorks } from "@/components/landing/how-it-works";
import { ProductBento } from "@/components/landing/product-bento";
import { ProofSection } from "@/components/landing/proof-section";
import { SecuritySection } from "@/components/landing/security-section";
import { SiteFooter } from "@/components/landing/site-footer";
import { SiteNav } from "@/components/landing/site-nav";
import { StandardsMarquee } from "@/components/landing/standards-marquee";

export const metadata: Metadata = {
  title: { absolute: "Countersign: the AI proposes, you countersign" },
  description:
    "An AI trading copilot for 021 Trade that cannot trade without your signature. It works with the 021 Developer Sandbox, and with a local paper broker for the Attack Lab and offline demos. The AI reads and drafts, a deterministic verifier checks, and you countersign the exact order with your passkey.",
};

/** Public landing page. A server component composing small client islands. */
export default function LandingPage() {
  return (
    <>
      <SiteNav />
      <main id="main">
        <Hero />
        <StandardsMarquee />
        <ProductBento />
        <HowItWorks />
        <BiometricsBand />
        <SecuritySection />
        <AttackLabSection />
        <ProofSection />
        <Faq />
        <FinalCta />
      </main>
      <SiteFooter />
    </>
  );
}
