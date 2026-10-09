import type { Metadata } from "next";
import { OutcomesView } from "@/components/outcomes/outcomes-view";

export const metadata: Metadata = { title: "Outcomes" };

export default function Page() {
  return <OutcomesView />;
}
