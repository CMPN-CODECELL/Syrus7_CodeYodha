"use client";

import { useEffect, useState } from "react";
import { Activity, Briefcase, ChartCandlestick, PanelRightOpen, Plus } from "lucide-react";
import {
  Conversation,
  ConversationContent,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { PromptInputProvider } from "@/components/ai-elements/prompt-input";
import { AuthorityChips } from "@/components/shell/authority-strip";
import { BrandMark } from "@/components/shell/brand";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useChatStore, useUiStore, type CopilotTab } from "@/lib/store";
import { Composer } from "./composer";
import { MessageItem, SuggestedPrompts } from "./messages";
import { RightPane } from "./right-pane";

function EmptyHero() {
  return (
    <div className="mx-auto flex w-full max-w-[880px] flex-col items-center gap-7 px-1 pt-6 pb-4 md:pt-12">
      <div className="flex flex-col items-center gap-3 text-center">
        <BrandMark className="size-10" />
        <h1 className="text-xl font-semibold tracking-tight md:text-2xl">What should we look at?</h1>
        <p className="max-w-lg text-sm text-muted-foreground">
          I read your account and the market, and I draft orders, plans and standing instructions. I cannot place an order: every trade
          waits for your countersignature.
        </p>
        <div className="flex flex-wrap justify-center gap-x-4 gap-y-1 font-mono text-[10.5px] tracking-wide text-subtle uppercase">
          <span className="text-ai">The AI can propose</span>
          <span className="text-info">The system can verify</span>
          <span className="text-human">The human must countersign</span>
        </div>
      </div>
      <SuggestedPrompts />
    </div>
  );
}

export function CopilotView() {
  const messages = useChatStore((s) => s.messages);
  const reset = useChatStore((s) => s.reset);
  const loadHistory = useChatStore((s) => s.loadHistory);
  const tab = useUiStore((s) => s.copilotTab);
  const setTab = useUiStore((s) => s.setCopilotTab);
  const [sheet, setSheet] = useState(false);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  const openPanel = (t: CopilotTab) => {
    setTab(t);
    setSheet(true);
  };

  return (
    <PromptInputProvider>
      <div className="flex h-full min-h-0">
        <section className="flex min-w-0 flex-1 flex-col" aria-label="Copilot conversation">
          <div className="flex h-11 shrink-0 items-center gap-2 border-b border-hairline px-3 md:px-6">
            <span className="hidden text-[13px] font-semibold sm:inline">Copilot</span>
            <AuthorityChips />
            <div className="ml-auto flex shrink-0 items-center gap-1">
              <Button variant="ghost" size="icon-sm" className="sm:hidden" aria-label="Open account, market and live outcomes" onClick={() => openPanel(tab)}>
                <PanelRightOpen />
              </Button>
              <div className="hidden items-center gap-1 sm:flex lg:hidden">
                <Button variant="ghost" size="icon-sm" aria-label="Account" onClick={() => openPanel("account")}>
                  <Briefcase />
                </Button>
                <Button variant="ghost" size="icon-sm" aria-label="Market" onClick={() => openPanel("market")}>
                  <ChartCandlestick />
                </Button>
                <Button variant="ghost" size="icon-sm" aria-label="Live outcomes" onClick={() => openPanel("outcomes")}>
                  <Activity />
                </Button>
              </div>
              {messages.length > 0 && (
                <Button variant="ghost" size="sm" onClick={reset} aria-label="New chat">
                  <Plus /> <span className="hidden sm:inline">New chat</span>
                </Button>
              )}
            </div>
          </div>

          {messages.length === 0 ? (
            <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-3 py-5 md:px-6">
              <EmptyHero />
            </div>
          ) : (
            <Conversation className="min-h-0 flex-1">
              <ConversationContent className="mx-auto w-full max-w-[920px] gap-6 px-3 py-5 md:px-6">
                {messages.map((m, i) => (
                  <MessageItem key={m.id} m={m} isLast={i === messages.length - 1} />
                ))}
              </ConversationContent>
              <ConversationScrollButton />
            </Conversation>
          )}

          <Composer />
        </section>

        <aside className="hidden w-[360px] shrink-0 border-l border-hairline bg-surface-1/40 lg:block xl:w-[400px]" aria-label="Account, market and live outcomes">
          <RightPane />
        </aside>

        <Sheet open={sheet} onOpenChange={setSheet}>
          <SheetContent side="bottom" className="pb-safe h-[82dvh] gap-0 rounded-t-2xl p-0 lg:hidden">
            <SheetTitle className="sr-only">Account, market and live outcomes</SheetTitle>
            <div className="mx-auto mt-2 h-1 w-10 rounded-full bg-hairline-strong" aria-hidden />
            <RightPane className="pt-1" />
          </SheetContent>
        </Sheet>
      </div>
    </PromptInputProvider>
  );
}
