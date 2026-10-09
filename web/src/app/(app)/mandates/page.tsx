import type { Metadata } from "next";
import { MandatesView } from "@/components/mandates/mandates-view";

export const metadata: Metadata = { title: "Mandates" };

export default function Page() {
  return <MandatesView />;
}
