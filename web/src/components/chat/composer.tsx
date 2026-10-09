"use client";

import { useEffect } from "react";
import { Cpu, PenLine } from "lucide-react";
import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  PromptInputSelect,
  PromptInputSelectContent,
  PromptInputSelectItem,
  PromptInputSelectTrigger,
  PromptInputSelectValue,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
  usePromptInputController,
} from "@/components/ai-elements/prompt-input";
import { useSecurityStatus } from "@/lib/queries";
import { useChatStore, useUiStore } from "@/lib/store";
import type { ChatEngine } from "@/lib/types";

const ENGINES: { value: ChatEngine; label: string }[] = [
  { value: "auto", label: "Auto" },
  { value: "ollama", label: "Ollama (local)" },
  { value: "gemini", label: "Gemini" },
  { value: "groq", label: "Groq" },
  { value: "deterministic", label: "Deterministic" },
];

/** Applies composer requests from elsewhere (Revise, clarify options, command palette). */
function ComposerBridge() {
  const controller = usePromptInputController();
  const send = useChatStore((s) => s.send);
  useEffect(() => {
    const apply = (req: ReturnType<typeof useUiStore.getState>["composerRequest"]) => {
      if (!req) return;
      useUiStore.getState().consumeComposer();
      if (req.send) {
        void send(req.text);
        return;
      }
      controller.textInput.setInput(req.text);
      requestAnimationFrame(() => {
        const ta = document.querySelector<HTMLTextAreaElement>("textarea[name='message']");
        ta?.focus();
        ta?.setSelectionRange(req.text.length, req.text.length);
      });
    };
    queueMicrotask(() => apply(useUiStore.getState().composerRequest));
    return useUiStore.subscribe((s, prev) => {
      if (s.composerRequest && s.composerRequest !== prev.composerRequest) apply(s.composerRequest);
    });
  }, [controller.textInput, send]);
  return null;
}

export function Composer() {
  const send = useChatStore((s) => s.send);
  const stop = useChatStore((s) => s.stop);
  const streaming = useChatStore((s) => s.streaming);
  const engine = useChatStore((s) => s.engine);
  const setEngine = useChatStore((s) => s.setEngine);
  const controller = usePromptInputController();
  const { data: sec } = useSecurityStatus();
  const available = (e: ChatEngine) => (e === "auto" || !sec ? true : sec.llm.available[e]);

  return (
    <div className="pb-safe shrink-0 border-t border-hairline bg-background/85 px-3 pt-3 pb-3 backdrop-blur md:px-6">
      <ComposerBridge />
      <div className="mx-auto w-full max-w-[880px]">
        <PromptInput
          onSubmit={(msg) => {
            const text = msg.text.trim();
            if (!text || streaming) return;
            void send(text);
          }}
          className="[&_[data-slot=input-group]]:rounded-xl [&_[data-slot=input-group]]:bg-surface-1 [&_[data-slot=input-group]]:ring-1 [&_[data-slot=input-group]]:ring-hairline-strong"
        >
          <PromptInputBody>
            <PromptInputTextarea
              placeholder="Ask about your portfolio, or describe a trade. The AI drafts; only you can countersign."
              className="min-h-14 text-[15px] md:text-sm"
              aria-label="Message the copilot"
            />
          </PromptInputBody>
          <PromptInputFooter>
            <PromptInputTools className="min-w-0 gap-1">
              <PromptInputSelect items={ENGINES} value={engine} onValueChange={(v) => v && setEngine(v as ChatEngine)}>
                <PromptInputSelectTrigger size="sm" aria-label="Model engine" className="h-8 gap-1.5 text-xs">
                  <Cpu className="size-3.5 text-ai" />
                  <PromptInputSelectValue />
                </PromptInputSelectTrigger>
                <PromptInputSelectContent alignItemWithTrigger={false} side="top" className="min-w-52">
                  {ENGINES.map((e) => (
                    <PromptInputSelectItem key={e.value} value={e.value} disabled={!available(e.value)}>
                      <span className="flex w-full items-center justify-between gap-3">
                        {e.label}
                        {!available(e.value) && <span className="text-[10px] text-muted-foreground">not configured</span>}
                      </span>
                    </PromptInputSelectItem>
                  ))}
                </PromptInputSelectContent>
              </PromptInputSelect>
              <span className="hidden items-center gap-1 truncate text-[11px] text-subtle sm:inline-flex">
                <PenLine className="size-3 text-human" />
                Drafts need your countersignature
              </span>
            </PromptInputTools>
            <PromptInputSubmit
              status={streaming ? "streaming" : undefined}
              onStop={stop}
              disabled={!streaming && !controller.textInput.value.trim()}
              className="size-9 rounded-lg bg-human text-background hover:bg-human/90 disabled:bg-foreground/10 disabled:text-muted-foreground"
            />
          </PromptInputFooter>
        </PromptInput>
      </div>
    </div>
  );
}
