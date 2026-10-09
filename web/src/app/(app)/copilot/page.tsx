import type { Metadata } from "next";
import { CopilotView } from "@/components/chat/copilot-view";

export const metadata: Metadata = { title: "Copilot" };

export default function CopilotPage() {
  return <CopilotView />;
}
