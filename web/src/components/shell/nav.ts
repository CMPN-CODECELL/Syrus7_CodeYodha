import {
  Activity,
  Briefcase,
  ChartCandlestick,
  Crosshair,
  MessageCircle,
  MessageSquare,
  ShieldCheck,
  Swords,
  Workflow,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  href: string;
  label: string;
  short: string;
  icon: LucideIcon;
  description: string;
}

export const NAV: NavItem[] = [
  { href: "/copilot", label: "Copilot", short: "Copilot", icon: MessageSquare, description: "Ask, draft and countersign" },
  { href: "/portfolio", label: "Portfolio", short: "Portfolio", icon: Briefcase, description: "Holdings, positions, orders" },
  { href: "/market", label: "Market", short: "Market", icon: ChartCandlestick, description: "Watchlist, chart, option chain" },
  { href: "/outcomes", label: "Outcomes", short: "Outcomes", icon: Activity, description: "Live execution timeline" },
  { href: "/mandates", label: "Mandates", short: "Mandates", icon: Crosshair, description: "Standing instructions" },
  { href: "/plans", label: "Plans", short: "Plans", icon: Workflow, description: "Multi-step plans" },
  { href: "/attack-lab", label: "Attack Lab", short: "Attacks", icon: Swords, description: "Proof under attack" },
  { href: "/alerts", label: "WhatsApp alerts", short: "Alerts", icon: MessageCircle, description: "Notifications only, never trades" },
  { href: "/security", label: "Security Center", short: "Security", icon: ShieldCheck, description: "Devices, kill switch, audit" },
];

export function isActive(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}
