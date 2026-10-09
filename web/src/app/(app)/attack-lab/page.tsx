import type { Metadata } from "next";
import { AttackLabView } from "@/components/attack-lab/attack-lab-view";

export const metadata: Metadata = { title: "Attack Lab" };

export default function Page() {
  return <AttackLabView />;
}
