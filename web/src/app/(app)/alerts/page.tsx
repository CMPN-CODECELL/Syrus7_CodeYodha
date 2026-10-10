import type { Metadata } from "next";
import { WhatsAppView } from "@/components/alerts/whatsapp-view";

export const metadata: Metadata = { title: "WhatsApp alerts" };

export default function Page() {
  return <WhatsAppView />;
}
