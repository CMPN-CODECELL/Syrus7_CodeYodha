import { displayFont } from "@/lib/fonts";
import "@/components/landing/landing.css";

/** Public marketing pages: editorial serif available as --font-display, normal document scrolling. */
export default function MarketingLayout({ children }: LayoutProps<"/">) {
  return (
    <div className={`${displayFont.variable} landing relative min-h-dvh overflow-x-clip bg-background text-foreground`}>
      {/* Without JavaScript nothing would reveal on scroll, so show everything. */}
      <noscript>
        <style>{".lp-reveal,.lp-rise{opacity:1!important;transform:none!important}"}</style>
      </noscript>
      {children}
    </div>
  );
}
