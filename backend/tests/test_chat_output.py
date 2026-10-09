"""The assistant's stored words are never a raw tool result, and the same question is never shown twice in a turn."""

from __future__ import annotations

from countersign.llm.chat import _dedupe_clarify, _tool_echo
from countersign.llm.tools import ask_clarification


def test_a_tool_result_echoed_as_the_reply_is_recognised():
    assert _tool_echo('{"status":"ASKED"}')
    assert _tool_echo(' {"status": "NEEDS_CLARIFICATION", "question": "x"} ')
    assert not _tool_echo("Your P&L today is ₹1,200.")
    assert not _tool_echo("{not json}")


def test_duplicate_and_empty_follow_up_questions_are_dropped():
    q = {"kind": "clarify", "question": "Did you mean RELIANCE?", "options": ["RELIANCE"]}
    cards = [q, {"kind": "clarify", "question": "did you mean  RELIANCE?", "options": ["RELIANCE"]}, {"kind": "proposal", "proposal_id": "p"}, {"kind": "clarify", "question": "Anything else?", "options": []}]
    assert _dedupe_clarify(cards) == [q, {"kind": "proposal", "proposal_id": "p"}]


def test_ask_tool_returns_a_sentence_not_json():
    shown: list[dict] = []

    class D:
        emit = shown.append

    out = ask_clarification(D(), "Market or limit?", ["at market"])
    assert isinstance(out, str) and not out.startswith("{") and shown[0]["question"] == "Market or limit?"
