import { Cpu, KeyRound, Smartphone } from "lucide-react";
import type { CSSProperties } from "react";
import { BrandMark } from "@/components/shell/brand";
import { cn } from "@/lib/utils";
import { Reveal } from "./reveal";
import { Accent, SectionHeading } from "./section-heading";
import { container } from "./styles";

// Fingerprint ridges from lucide's fingerprint-pattern icon (ISC licence), drawn here so each
// ridge can light up in turn.
const RIDGES = [
  "M2 12a10 10 0 0 1 18-6",
  "M21.8 16c.2-2 .131-5.354 0-6",
  "M9 6.8a6 6 0 0 1 9 5.2v2",
  "M5 19.5C5.5 18 6 15 6 12a6 6 0 0 1 .34-2",
  "M17.29 21.02c.12-.6.43-2.3.5-3.02",
  "M12 10a2 2 0 0 0-2 2c0 1.02-.1 2.51-.26 4",
  "M14 13.12c0 2.38 0 6.38-1 8.88",
  "M8.65 22c.21-.66.45-1.32.57-2",
];

const POINTS = [
  {
    icon: Cpu,
    title: "Checked on your device",
    body: "Your computer's platform authenticator verifies you locally. Countersign never receives, stores or sees your face or fingerprint.",
  },
  {
    icon: KeyRound,
    title: "Only a signature leaves",
    body: "Your device signs the order's SHA-256 hash with a private key that never leaves it. The server verifies the signature.",
  },
  {
    icon: Smartphone,
    title: "Fallbacks you control",
    body: "No biometrics on this device? Use the 4-digit app PIN you choose, or a 6-digit code from an authenticator app.",
  },
];

// What this build can use: passkeys are bound to http://localhost:3000, so only the computer running it can sign.
const PLATFORMS = ["Windows Hello", "Touch ID"];

function DevicePrompt() {
  return (
    <div className="relative mx-auto w-full max-w-[400px]">
      <div aria-hidden className="absolute -inset-10 -z-10 rounded-full bg-[radial-gradient(closest-side,var(--lp-glow),transparent)]" />
      <div className="rounded-[26px] border border-hairline-strong bg-background/85 p-6 shadow-[0_40px_100px_-40px_rgba(0,0,0,0.7)] backdrop-blur-xl sm:p-7">
        <div className="flex items-center gap-2.5">
          <BrandMark className="size-6" />
          <span className="text-[13px] font-medium text-muted-foreground">Countersign</span>
        </div>
        <p className="mt-5 text-[19px] font-semibold tracking-tight">Verify it&apos;s you</p>
        <p className="mt-1 text-[13.5px] leading-snug text-muted-foreground">
          to countersign <span className="num font-semibold text-foreground">BUY 10 RELIANCE @ ₹2,800.00</span>
        </p>
        <div className="my-7 grid place-items-center">
          <div className="relative grid size-28 place-items-center rounded-full bg-human/[0.07] ring-1 ring-human/25">
            <span className="lp-ping absolute inset-3 rounded-full bg-human/10" aria-hidden />
            <svg viewBox="0 0 24 24" className="relative size-16 text-human" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              {RIDGES.map((d) => (
                <path key={d} d={d} opacity={0.18} />
              ))}
              {RIDGES.map((d, i) => (
                <path key={`lit-${d}`} d={d} pathLength={1} className="lp-ridge" style={{ "--i": i } as CSSProperties} />
              ))}
            </svg>
          </div>
          <p className="mt-4 text-[12.5px] text-muted-foreground">Use Windows Hello or Touch ID on this computer</p>
        </div>
        <div className="rounded-xl bg-foreground/[0.04] px-3.5 py-2.5 ring-1 ring-hairline">
          <p className="text-[10.5px] font-semibold tracking-[0.14em] text-subtle uppercase">Signing</p>
          <p className="num mt-0.5 truncate text-[12px] text-foreground/85">sha256 5757 8100 c096 … 1d35 c526</p>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-2 text-[13px] font-medium" aria-hidden>
          <span className="grid h-10 place-items-center rounded-lg border border-hairline-strong text-muted-foreground">Use PIN instead</span>
          <span className="grid h-10 place-items-center rounded-lg border border-hairline-strong text-muted-foreground">Cancel</span>
        </div>
      </div>
    </div>
  );
}

export function BiometricsBand() {
  return (
    <section id="biometrics" aria-labelledby="bio-title" className="relative overflow-hidden py-24 sm:py-32">
      <div className={cn(container, "grid items-center gap-16 lg:grid-cols-12")}>
        <div className="lg:col-span-6">
          <SectionHeading
            id="bio-title"
            label="Device biometrics"
            title={
              <>
                Approve with this computer&apos;s <Accent>passkey.</Accent>
              </>
            }
            lede="Countersign uses your computer's built-in platform authenticator through WebAuthn passkeys, so approving a trade feels like unlocking your laptop with Windows Hello or Touch ID."
          />
          <Reveal delay={80}>
            <ul className="mt-10 space-y-6">
              {POINTS.map((p) => (
                <li key={p.title} className="flex gap-4">
                  <span className="grid size-10 shrink-0 place-items-center rounded-xl border border-hairline-strong bg-background/60 text-human">
                    <p.icon className="size-[18px]" aria-hidden />
                  </span>
                  <div>
                    <h3 className="text-[16px] font-semibold">{p.title}</h3>
                    <p className="mt-1 text-[14.5px] leading-relaxed text-muted-foreground">{p.body}</p>
                  </div>
                </li>
              ))}
            </ul>
          </Reveal>
          <div className="mt-9 flex flex-wrap items-center gap-2">
            <span className="text-[12.5px] text-muted-foreground">On this computer:</span>
            <ul className="flex flex-wrap gap-2" aria-label="On this computer">
              {PLATFORMS.map((p) => (
                <li key={p} className="rounded-full border border-hairline-strong px-3 py-1 text-[12.5px] text-muted-foreground">
                  {p}
                </li>
              ))}
            </ul>
          </div>
          <p className="mt-3 max-w-md text-[12.5px] leading-relaxed text-muted-foreground">
            Phones (Face ID, Android fingerprint) need an HTTPS deployment with a matching domain, which this local build does not
            have. See the project&apos;s limitations.
          </p>
        </div>
        <Reveal className="lg:col-span-6" delay={120}>
          <DevicePrompt />
        </Reveal>
      </div>
    </section>
  );
}
