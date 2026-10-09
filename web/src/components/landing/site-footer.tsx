import { BrandMark } from "@/components/shell/brand";
import { cn } from "@/lib/utils";
import { ThemeSwitch } from "./controls";
import { AppLink } from "./cta";
import { FooterStatus } from "./footer-status";
import { container } from "./styles";

const link = "text-[14px] text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring rounded-sm";
const heading = "font-display text-[19px] text-foreground";

export function SiteFooter() {
  return (
    <footer className="relative px-4 pb-8 sm:px-6 lg:px-8">
      <div className={cn(container, "overflow-hidden rounded-[28px] border border-hairline bg-surface-1/40 px-6 pt-14 pb-8 sm:px-10 lg:px-14")}>
        <div className="grid gap-12 lg:grid-cols-12">
          <div className="lg:col-span-4">
            <div className="flex items-center gap-2.5">
              <BrandMark className="size-8" />
              <span className="text-[17px] font-semibold tracking-tight">Countersign</span>
            </div>
            <p className="mt-4 max-w-xs text-[14px] leading-relaxed text-muted-foreground">
              An AI trading copilot that cannot trade without your signature.
            </p>
          </div>
          <nav aria-label="Footer" className="grid grid-cols-2 gap-10 sm:grid-cols-4 lg:col-span-8">
            <div>
              <p className={heading}>Product</p>
              <ul className="mt-4 space-y-3">
                <li><a className={link} href="#product">Overview</a></li>
                <li><a className={link} href="#how-it-works">How it works</a></li>
                <li><AppLink to="/copilot" className={link}>Open the Copilot</AppLink></li>
                <li><a className={link} href="#faq">FAQ</a></li>
              </ul>
            </div>
            <div>
              <p className={heading}>Security</p>
              <ul className="mt-4 space-y-3">
                <li><a className={link} href="#security">Live monitor</a></li>
                <li><a className={link} href="#biometrics">Device biometrics</a></li>
                <li><a className={link} href="#proof">Proof</a></li>
                <li><AppLink to="/security" className={link}>Security Center</AppLink></li>
              </ul>
            </div>
            <div>
              <p className={heading}>Project</p>
              <ul className="mt-4 space-y-3 text-[14px] text-muted-foreground">
                <li>SYRUS 7.0</li>
                <li>PS-04, AI Trading Copilot</li>
                <li>Team CodeYodha</li>
              </ul>
            </div>
            <div>
              <p className={heading}>In the repo</p>
              {/* File names, not links: the docs ship with the source code. */}
              <ul className="mt-4 space-y-3 text-[13px] text-subtle">
                <li>
                  <span className="num text-foreground/80">SETUP.md</span>
                  <span className="block text-[12px]">Setup guide</span>
                </li>
                <li>
                  <span className="num text-foreground/80">docs/SECURITY.md</span>
                  <span className="block text-[12px]">Security model</span>
                </li>
                <li>
                  <span className="num text-foreground/80">docs/ATTACK_LAB.md</span>
                  <span className="block text-[12px]">
                    Scenarios, also live in the <AppLink to="/attack-lab" className={cn(link, "text-[12px] underline underline-offset-2")}>Attack Lab</AppLink>
                  </span>
                </li>
              </ul>
            </div>
          </nav>
        </div>

        <div className="mt-14 flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
          <FooterStatus />
          <ThemeSwitch className="w-fit" />
        </div>

        <div className="mt-6 flex flex-col gap-3 border-t border-hairline pt-6 text-[13px] text-subtle sm:flex-row sm:items-center sm:justify-between">
          <p>© 2026 Team CodeYodha. Built for SYRUS 7.0.</p>
          <p className="font-display text-[17px] text-muted-foreground">The AI proposes. You countersign.</p>
        </div>

        <p
          aria-hidden
          className="font-display pointer-events-none mt-10 -mb-[0.32em] text-center text-[clamp(4rem,17vw,15rem)] leading-none bg-[linear-gradient(180deg,color-mix(in_oklch,var(--foreground)_16%,transparent),transparent_80%)] bg-clip-text text-transparent select-none"
        >
          Countersign
        </p>
      </div>
    </footer>
  );
}
