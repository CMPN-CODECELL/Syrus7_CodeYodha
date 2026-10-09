"""Chat orchestrator: streams typed events (meta, tool, text-delta, card, done, error) for the web chat."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import uuid
from collections.abc import AsyncIterator

from countersign.core import audit
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import ChatMessage
from countersign.core.parser import parse
from countersign.llm import router
from countersign.llm.agent import build_agent, engine_label, resolve_engine
from countersign.llm.tools import ChatDeps

_HISTORY: dict[tuple[str, str], list] = {}

TOOL_LABELS = {
    "get_account_summary": "Reading account summary",
    "get_holdings": "Reading holdings",
    "get_positions": "Reading positions",
    "get_orders": "Reading orders",
    "get_quote": "Fetching live quote",
    "get_option_chain": "Building NIFTY option chain",
    "find_instrument": "Resolving instrument",
    "propose_order": "Drafting order for deterministic verification",
    "propose_cancel": "Drafting cancellation for verification",
    "propose_modify": "Drafting modification for verification",
    "propose_modify_stop_loss": "Drafting stop-loss modification for verification",
    "propose_plan": "Drafting plan for verification",
    "propose_exit_losing_positions": "Computing exits for losing positions",
    "propose_rebalance": "Computing a sells-only rebalance",
    "propose_standing_instruction": "Drafting standing instruction for verification",
    "ask_clarification": "Asking you to clarify",
}


THINK_OPEN, THINK_CLOSE = "<think>", "</think>"
# Where a run-on reasoning block meets the answer: sentence punctuation directly followed by a capital letter.
_SEAM = re.compile(r"[.!?](?=[A-Z])")


class ReasoningFilter:
    """Keeps a model's private reasoning out of the chat. Some reasoning models (or a model imitating earlier turns)
    write a "<think>" block as ordinary text; it is dropped. If the closing tag never arrives, only the text after the
    last sentence seam ("...for clarification.Which product...") is kept."""

    def __init__(self) -> None:
        self.buf = ""
        self.mode = "start"  # start: undecided, think: inside a reasoning block, text: passing through

    def feed(self, chunk: str) -> str:
        if self.mode == "text":
            return chunk
        self.buf += chunk
        if self.mode == "start":
            head = self.buf.lstrip()
            if head.startswith(THINK_OPEN):
                self.mode, self.buf = "think", head[len(THINK_OPEN) :]
            elif THINK_OPEN.startswith(head):
                return ""  # could still become "<think>"
            else:
                self.mode, out, self.buf = "text", self.buf, ""
                return out
        end = self.buf.find(THINK_CLOSE)
        if end < 0:
            return ""
        self.mode, out, self.buf = "text", self.buf[end + len(THINK_CLOSE) :].lstrip(), ""
        return out

    def finish(self) -> str:
        out = ""
        if self.mode == "start":
            out = self.buf
        elif self.mode == "think":
            seams = list(_SEAM.finditer(self.buf))
            out = self.buf[seams[-1].end() :] if seams else ""
        self.buf = ""
        return out


def strip_reasoning(text: str) -> str:
    f = ReasoningFilter()
    return (f.feed(text) + f.finish()).strip()


def _clean_history(messages: list) -> list:
    """Model memory without reasoning: replaying earlier "<think>" blocks invites the model to write them again."""
    from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart

    out = []
    for m in messages:
        if isinstance(m, ModelResponse):
            parts = [dataclasses.replace(x, content=strip_reasoning(x.content)) if isinstance(x, TextPart) else x for x in m.parts if not isinstance(x, ThinkingPart)]
            m = dataclasses.replace(m, parts=parts or m.parts)
        out.append(m)
    return out


def _deterministic(deps: ChatDeps) -> str:
    """The deterministic engine, with a broker that is slow or refusing (the 021 sandbox injects timeouts, rate limits
    and 5xx on purpose) answered as what it is instead of a failed assistant. Read-only paths never retry writes."""
    from countersign.core.broker_client import BrokerError, BrokerTimeout, BrokerUnavailable

    try:
        return router.handle(deps)
    except (BrokerTimeout, BrokerUnavailable) as e:
        deps.emit({"kind": "notice", "tone": "warning", "title": "The broker did not answer in time", "message": f"{type(e).__name__}: nothing was changed. Ask again in a few seconds."})
        return "The broker did not answer in time, so I cannot answer that reliably right now. Nothing was changed; please ask again in a few seconds."
    except BrokerError as e:
        deps.emit({"kind": "notice", "tone": "warning", "title": "The broker refused the request", "message": f"{e.code}: {e.message}"[:300]})
        return "The broker refused that request, so I cannot answer it right now. Nothing was changed."


def _after_interruption(deps: ChatDeps) -> tuple[str, list[dict]]:
    """When the model stops after drafting, the trader still gets the verifier's own words: what was drafted, why it is
    blocked, and for a market order the AI chose on its own, the two complete answers to click."""
    from countersign.core import market
    from countersign.core.models import Proposal
    from countersign.core.money import round_to_tick
    from countersign.core.verifier import _rupees_text

    lines: list[str] = []
    cards: list[dict] = []
    with session_scope() as s:
        for pid in deps.proposal_ids:
            p = s.get(Proposal, pid)
            if p is None:
                continue
            blocks = [i for sv in (p.steps_view or []) for i in sv.get("issues", []) if i.get("severity") == "BLOCK"] + [i for i in (p.issues or []) if i.get("severity") == "BLOCK"]
            if not blocks:
                lines.append(f"Drafted: {p.summary}. It is ready for your countersignature.")
                continue
            lines.append(f"Drafted: {p.summary}. It is blocked: " + " ".join(dict.fromkeys(i.get("message", "") for i in blocks)))
            c = p.canonical if isinstance(p.canonical, dict) else {}
            inst = c.get("instrument") or {}
            if p.kind == "ORDER" and any(i.get("code") == "ORDER_TYPE_NOT_STATED" for i in blocks) and c.get("order_type") == "MARKET" and inst.get("symbol"):
                side, qty, sym = str(c.get("side", "")).capitalize(), c.get("quantity"), inst["symbol"]
                opts = [f"{side} {qty} {sym} at market"]
                t = market.latest(inst.get("instrument_id", ""))
                if t is not None:
                    px = round_to_tick(t.ltp_paise, int(inst.get("tick_size_paise") or 5), "up" if side == "Buy" else "down")
                    opts.append(f"{side} {qty} {sym} at limit {_rupees_text(px)}")
                cards.append({"kind": "clarify", "question": "Market or limit? You did not say, so this draft cannot be countersigned as it is.", "options": opts})
    return " ".join(lines), cards


def _dedupe_clarify(cards: list[dict]) -> list[dict]:
    """A model sometimes asks the same question twice, or adds an empty question after the verifier already asked."""
    seen: set[str] = set()
    out = []
    asked = any(c.get("kind") == "clarify" and c.get("options") for c in cards)
    for c in cards:
        if c.get("kind") == "clarify":
            key = " ".join(str(c.get("question", "")).lower().split())
            if key in seen or (asked and not c.get("options") and c is not next(x for x in cards if x.get("kind") == "clarify")):
                continue
            seen.add(key)
        out.append(c)
    return out


def _tool_echo(text: str) -> bool:
    """True when the model's whole reply is a tool result echoed back (for example {"status":"ASKED"})."""
    t = text.strip()
    if not (t.startswith("{") and t.endswith("}")):
        return False
    try:
        import json

        return isinstance(json.loads(t), dict)
    except ValueError:
        return False


async def stream(user_id: str, account_id: str, conversation_id: str | None, message: str, engine: str = "auto") -> AsyncIterator[dict]:
    conversation_id = conversation_id or str(uuid.uuid4())
    message = message.strip()[:2000]
    parsed = parse(message) if (engine or "auto").lower() == "auto" and engine_label("auto") != "deterministic" else None
    if parsed is not None and parsed.kind == "query":
        # Read-only questions the deterministic interpreter recognises with certainty are answered on a fast
        # path (sub-second, no model cost).
        model, label = None, "deterministic (fast path for read-only query)"
    elif parsed is not None and parsed.kind == "mandate":
        # Standing instructions the deterministic parser fully understood ("If Reliance drops below 2800, buy 10",
        # "Tell me when any holding falls 3% in a day") never go through a model: a model once turned the conditional
        # buy into an alert and dropped "buy 10". The verifier and the countersignature are the same either way.
        model, label = None, "deterministic (fast path for a standing instruction)"
    else:
        model, label = resolve_engine(engine)
    msg_id = str(uuid.uuid4())
    yield {"type": "meta", "conversation_id": conversation_id, "engine": label, "message_id": msg_id}
    with session_scope() as s:
        s.add(ChatMessage(conversation_id=conversation_id, user_id=user_id, role="user", text=message, cards=[]))
        audit.record(s, "trader", "USER_REQUEST", "conversation", conversation_id, {"text": message, "engine": label})

    cards: list[dict] = []
    pending: list[dict] = []
    deps = ChatDeps(user_id=user_id, account_id=account_id, conversation_id=conversation_id, user_text=message, engine=label, emit=pending.append)
    text_parts: list[str] = []

    async def flush():
        while pending:
            c = pending.pop(0)
            if _dedupe_clarify([*cards, c])[-1] is not c:
                continue  # the same question was already shown in this turn
            cards.append(c)
            yield {"type": "card", "card": c}

    if model is None:
        yield {"type": "tool", "id": "det", "name": "deterministic_interpreter", "label": "Interpreting your request (deterministic engine)", "status": "running"}
        text = await asyncio.to_thread(_deterministic, deps)
        yield {"type": "tool", "id": "det", "name": "deterministic_interpreter", "label": "Interpreted", "status": "done"}
        async for e in flush():
            yield e
        for i in range(0, len(text), 24):
            yield {"type": "text-delta", "delta": text[i : i + 24]}
            await asyncio.sleep(0.01)
        text_parts.append(text)
    else:
        agent = build_agent(model)
        # Model memory is keyed by user too: a conversation id from another trader never loads their context.
        history = _HISTORY.get((user_id, conversation_id), [])
        failed = None
        reasoning = ReasoningFilter()
        try:
            async with asyncio.timeout(settings().llm_timeout_s):
                async with agent.run_stream_events(message, deps=deps, message_history=history) as stream:
                    async for ev in stream:
                        name = type(ev).__name__
                        piece = ""
                        if name == "PartStartEvent":
                            part = ev.part
                            if type(part).__name__ == "TextPart" and part.content:
                                piece = reasoning.feed(part.content)
                        elif name == "PartDeltaEvent":
                            delta = ev.delta
                            if type(delta).__name__ == "TextPartDelta" and delta.content_delta:
                                piece = reasoning.feed(delta.content_delta)
                        elif name == "FunctionToolCallEvent":
                            p = ev.part
                            yield {"type": "tool", "id": p.tool_call_id, "name": p.tool_name, "label": TOOL_LABELS.get(p.tool_name, p.tool_name), "status": "running"}
                        elif name == "FunctionToolResultEvent":
                            r = getattr(ev, "part", None) or getattr(ev, "result", None)
                            tid = getattr(r, "tool_call_id", "")
                            tname = getattr(r, "tool_name", "") or ""
                            ok = type(r).__name__ != "RetryPromptPart"
                            yield {"type": "tool", "id": tid, "name": tname, "label": TOOL_LABELS.get(tname, tname), "status": "done" if ok else "error"}
                            async for e in flush():
                                yield e
                        elif name == "AgentRunResultEvent":
                            try:
                                _HISTORY[(user_id, conversation_id)] = _clean_history(list(ev.result.all_messages())[-16:])
                            except Exception:  # noqa: BLE001
                                pass
                        if piece:
                            text_parts.append(piece)
                            yield {"type": "text-delta", "delta": piece}
        except Exception as e:  # noqa: BLE001
            failed = e
        if tail := reasoning.finish():
            text_parts.append(tail)
            yield {"type": "text-delta", "delta": tail}
        async for e in flush():
            yield e
        if failed is not None and not deps.proposal_ids and not cards:
            yield {"type": "card", "card": {"kind": "notice", "tone": "warning", "title": "AI engine unavailable", "message": f"{label} did not respond ({type(failed).__name__}). Answered with the deterministic engine instead; safety is unchanged."}}
            deps.engine = "deterministic"
            text = await asyncio.to_thread(_deterministic, deps)
            async for e in flush():
                yield e
            text_parts = [text]
            yield {"type": "text-delta", "delta": text}
        elif failed is not None:
            yield {"type": "card", "card": {"kind": "notice", "tone": "warning", "title": "AI response interrupted", "message": "The model stopped early. Anything it drafted is shown above and still requires your countersignature."}}
            explain, extra = await asyncio.to_thread(_after_interruption, deps)
            pending.extend(extra)
            async for e in flush():
                yield e
            if explain and not "".join(text_parts).strip():
                text_parts = [explain]
                yield {"type": "text-delta", "delta": explain}
    final_text = "".join(text_parts).strip()
    if _tool_echo(final_text):
        final_text = ""  # never store a raw tool result as the assistant's words
    with session_scope() as s:
        s.add(ChatMessage(conversation_id=conversation_id, user_id=user_id, role="assistant", text=final_text, cards=cards, engine=label))
    yield {"type": "done"}


def forget(user_id: str, conversation_ids: list[str]) -> None:
    """Drops a trader's in-memory model context (used by the per-user demo reset)."""
    for cid in conversation_ids:
        _HISTORY.pop((user_id, cid), None)
