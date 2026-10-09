import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Reveal } from "./reveal";
import { eyebrow } from "./styles";

/** Editorial section heading: eyebrow, large serif title (accent words in mint), short lede, optional aside. */
export function SectionHeading({
  id,
  label,
  title,
  lede,
  aside,
  className,
  center,
}: {
  id: string;
  label: string;
  title: ReactNode;
  lede?: ReactNode;
  aside?: ReactNode;
  className?: string;
  center?: boolean;
}) {
  return (
    <Reveal
      className={cn(
        "flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between",
        center && "items-center text-center lg:flex-col lg:items-center",
        className,
      )}
    >
      <div className={cn("max-w-3xl", center && "mx-auto")}>
        <p className={eyebrow}>{label}</p>
        <h2 id={id} className="font-display mt-4 text-[clamp(2.5rem,6vw,4.25rem)] leading-[1.02] text-foreground">
          {title}
        </h2>
        {lede && <p className="mt-5 max-w-2xl text-[17px] leading-relaxed text-muted-foreground sm:text-lg">{lede}</p>}
      </div>
      {aside && <div className="shrink-0">{aside}</div>}
    </Reveal>
  );
}

/** Mint accent words inside a serif title. */
export function Accent({ children }: { children: ReactNode }) {
  return <span className="text-human">{children}</span>;
}
