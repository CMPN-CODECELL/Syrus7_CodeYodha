"""The deterministic engine (offline mode and the fallback when a model fails) drafts every new L2 to L4 request
through the same verifier, and says plainly when there is nothing to draft."""

from countersign.llm import router
from countersign.llm import tools as T
from tests.test_l2_l4_support import Gw, holding, ltp, position, proposal_count, rupees, user_id, view


def _say(text: str, gw: Gw) -> tuple[str, list[dict]]:
    cards: list[dict] = []
    d = T.ChatDeps(user_id=user_id(), account_id="LAB1", conversation_id="c-r", user_text=text, engine="deterministic", emit=cards.append, origin="test", gateway=gw)
    return router.handle(d), cards


def _proposal(cards: list[dict]) -> dict:
    ids = [c["proposal_id"] for c in cards if c["kind"] == "proposal"]
    assert len(ids) == 1, cards
    return view(ids[0])


def test_engine_drafts_each_new_request_kind():
    p, t = ltp("TCS")
    pi, ti = ltp("ICICIBANK")
    gw = Gw(holdings=[holding("TCS", 25, p)], positions=[position("ICICIBANK", "MIS", 40, pi + 10 * ti)])
    text, cards = _say("Sell half my TCS at market", gw)
    v = _proposal(cards)
    assert v["steps"][0]["canonical"]["quantity"] == 12 and "countersign" in text
    text, cards = _say("Exit all my losing intraday positions", gw)
    v = _proposal(cards)
    assert v["kind"] == "PLAN" and "no compensating trades" in text
    text, cards = _say("Tell me when any holding falls 3% in a day", gw)
    v = _proposal(cards)
    assert v["kind"] == "MANDATE" and "once per holding per trading day" in text
    r, _ = ltp("RELIANCE")
    text, cards = _say(f"If Reliance drops below {rupees(r - 30 * 10)}, buy 10", gw)
    assert _proposal(cards)["kind"] == "MANDATE" and "fires at most once" in text


def test_engine_explains_when_nothing_is_drafted():
    p, t = ltp("TCS")
    gw = Gw(holdings=[holding("TCS", 25, p)])
    before = proposal_count()
    text, cards = _say("Exit all my losing intraday positions", gw)
    assert "no open intraday (MIS) positions" in text and cards[-1]["kind"] == "notice"
    text, cards = _say("Rebalance so no single stock is more than 20% of my portfolio", gw)
    assert "less than 100%" in text
    text, cards = _say("Sell half my TCS", gw)
    assert cards[-1]["kind"] == "clarify" and cards[-1]["options"] == ["Sell half my TCS at market"]
    assert proposal_count() == before
