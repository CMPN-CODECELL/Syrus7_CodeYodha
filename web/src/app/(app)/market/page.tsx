import type { Metadata } from "next";
import { MarketView } from "@/components/market/market-view";

export const metadata: Metadata = { title: "Market" };

export default function Page() {
  return <MarketView />;
}
