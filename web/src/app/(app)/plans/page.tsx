import type { Metadata } from "next";
import { PlansView } from "@/components/plans/plans-view";

export const metadata: Metadata = { title: "Plans" };

export default function Page() {
  return <PlansView />;
}
