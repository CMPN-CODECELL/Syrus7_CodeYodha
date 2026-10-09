"use client";

import { motion, useReducedMotion } from "motion/react";
import { Check, CircleAlert, MessageSquare, PenLine, Sparkles, Square, X } from "lucide-react";
import { Message, MessageContent, MessageResponse } from "@/components/ai-elements/message";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { CardRenderer } from "@/components/cards/cards";
import { engineLabel } from "@/components/approval/approval-card";
import { SUGGESTED_PROMPTS } from "@/components/shell/command-palette";
import { Chip } from "@/components/ui/chip";
import { time } from "@/lib/format";
import { useChatStore, type ChatMessage, type ChatPart, type ToolActivity } from "@/lib/store";
import { cn } from "@/lib/utils";

export function ToolChip({ tool }: { tool: ToolActivity }) {
  const running = tool.status === "running";
  const label = running ? tool.label : tool.label.replace(/\.\.\.$/, "");
  return (
    <span
      title={tool.detail ? `${tool.name}: ${tool.detail}` : tool.name}
      className={cn(
        "inline-flex h-7 max-w-full items-center gap-1.5 rounded-full px-2.5 text-xs ring-1 ring-inset",
        running && "bg-ai/[0.08] ring-ai/30",
        tool.status === "done" && "bg-surface-2 text-muted-foreground ring-hairline",
        tool.status === "error" && "bg-danger/10 text-danger ring-danger/30",
      )}
    >
      {running ? (
        <span className="relative flex size-2">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-ai opacity-60" />
          <span className="relative inline-flex size-2 rounded-full bg-ai" />
        </span>
      ) : tool.status === "done" ? (
        <Check className="size-3 text-profit" />
      ) : (
        <X className="size-3" />
      )}
      {running ? (
        <Shimmer as="span" className="truncate text-xs" duration={1.6}>
          {label}
        </Shimmer>
      ) : (
        <span className="truncate">{label}</span>
      )}
      {tool.detail && !running && <span className="hidden truncate text-subtle sm:inline">· {tool.detail}</span>}
    </span>
  );
}

function groupParts(parts: ChatPart[]) {
  // Consecutive tool parts render as one wrapping row of chips.
  const groups: ({ kind: "tools"; tools: ToolActivity[]; key: string } | { kind: "part"; part: ChatPart; key: string })[] = [];
  parts.forEach((p, i) => {
    const last = groups[groups.length - 1];
    if (p.type === "tool") {
      if (last && last.kind === "tools") last.tools.push(p.tool);
      else groups.push({ kind: "tools", tools: [p.tool], key: `tools-${i}` });
    } else groups.push({ kind: "part", part: p, key: p.type === "card" ? p.key || `card-${i}` : `text-${i}` });
  });
  return groups;
}

function AssistantMessage({ m, isLast }: { m: ChatMessage; isLast: boolean }) {
  const groups = groupParts(m.parts);
  const eng = m.engine ? engineLabel(m.engine) : null;
  const working = m.status === "pending" || m.status === "streaming";
  return (
    <Message from="assistant" className="max-w-full">
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <span className="grid size-5 place-items-center rounded-md bg-ai/15 text-ai ring-1 ring-ai/30">
          <Sparkles className="size-3" />
        </span>
        <span className="font-medium text-foreground/80">Copilot</span>
        {eng && (
          <Chip tone={eng.tone} size="xs" className="max-w-[240px] normal-case" title={eng.full}>
            <span className="truncate">{eng.label}</span>
          </Chip>
        )}
        <span className="text-subtle">read / prepare only</span>
        <span className="num ml-auto text-subtle">{time(m.createdAt)}</span>
      </div>
      <MessageContent className="w-full max-w-full gap-3">
        {m.status === "pending" && m.parts.length === 0 && (
          <Shimmer as="p" className="text-sm" duration={1.6}>
            Reading your request...
          </Shimmer>
        )}
        {groups.map((g) =>
          g.kind === "tools" ? (
            <div key={g.key} className="flex flex-wrap gap-1.5">
              {g.tools.map((t) => (
                <ToolChip key={t.id} tool={t} />
              ))}
            </div>
          ) : g.part.type === "text" ? (
            <MessageResponse key={g.key} className="text-[14px] leading-relaxed" isAnimating={working && isLast}>
              {g.part.text}
            </MessageResponse>
          ) : g.part.type === "card" ? (
            <div key={g.key} className="w-full min-w-0">
              <CardRenderer card={g.part.card} />
            </div>
          ) : null,
        )}
        {m.status === "error" && m.error && (
          <div role="alert" className="flex items-start gap-2 rounded-lg bg-danger/[0.08] px-3 py-2.5 text-[13px] ring-1 ring-danger/30">
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" />
            <div>
              <p className="text-foreground">{m.error.message}</p>
              <p className="num mt-0.5 text-[10.5px] text-subtle">{m.error.code}</p>
            </div>
          </div>
        )}
        {m.status === "stopped" && (
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Square className="size-3" /> Stopped. Nothing was drafted after this point.
          </p>
        )}
      </MessageContent>
    </Message>
  );
}

export function MessageItem({ m, isLast }: { m: ChatMessage; isLast: boolean }) {
  const reduce = useReducedMotion();
  return (
    <motion.div
      initial={reduce ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
    >
      {m.role === "user" ? (
        <Message from="user">
          <MessageContent className="max-w-[85%] rounded-2xl rounded-br-md bg-surface-3! px-3.5 py-2.5 text-[14px] ring-1 ring-hairline">
            {m.parts.map((p, i) => (p.type === "text" ? <p key={i} className="whitespace-pre-wrap">{p.text}</p> : null))}
          </MessageContent>
        </Message>
      ) : (
        <AssistantMessage m={m} isLast={isLast} />
      )}
    </motion.div>
  );
}

export function SuggestedPrompts() {
  const send = useChatStore((s) => s.send);
  const streaming = useChatStore((s) => s.streaming);
  const reduce = useReducedMotion();
  const groups = [
    {
      title: "Ask (read only)",
      hint: "The copilot reads your account and the market",
      icon: MessageSquare,
      tone: "text-ai",
      ring: "hover:ring-ai/45",
      items: SUGGESTED_PROMPTS.read,
    },
    {
      title: "Draft for your countersignature",
      hint: "Nothing executes until you sign",
      icon: PenLine,
      tone: "text-human",
      ring: "hover:ring-human/45",
      items: SUGGESTED_PROMPTS.propose,
    },
  ];
  return (
    <div className="grid w-full gap-4 md:grid-cols-2">
      {groups.map((g, gi) => (
        <div key={g.title} className="space-y-2">
          <div className="flex items-center gap-2">
            <g.icon className={cn("size-3.5 shrink-0", g.tone)} />
            <span className="shrink-0 text-xs font-semibold">{g.title}</span>
            <span className="hidden truncate text-[11px] text-subtle sm:inline">{g.hint}</span>
          </div>
          <div className="grid gap-1.5">
            {g.items.map((p, i) => (
              <motion.button
                key={p}
                type="button"
                disabled={streaming}
                initial={reduce ? false : { opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.05 * (gi * 4 + i), duration: 0.2 }}
                onClick={() => void send(p)}
                className={cn(
                  "group flex min-h-11 items-center justify-between gap-3 rounded-lg bg-surface-1 px-3.5 text-left text-[13.5px] ring-1 ring-hairline transition-colors disabled:opacity-50",
                  g.ring,
                )}
              >
                <span>{p}</span>
                <span className="text-subtle opacity-0 transition-opacity group-hover:opacity-100" aria-hidden>
                  Enter
                </span>
              </motion.button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
