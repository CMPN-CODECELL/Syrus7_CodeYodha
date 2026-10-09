"""L1 read-only questions from 021-01 route to the right read tools in the deterministic engine (also the fast path
the chat uses for recognised questions with any model)."""

from countersign.core.money import inr
from countersign.llm import router
from countersign.llm import tools as T
from tests.test_l2_l4_support import Gw, holding, inst, ltp, position, user_id


def _ask(text: str, gw: Gw) -> tuple[str, list[dict]]:
    cards: list[dict] = []
    d = T.ChatDeps(user_id=user_id(), account_id="LAB1", conversation_id="c-l1", user_text=text, engine="deterministic", emit=cards.append, origin="test", gateway=gw)
    return router.handle(d), cards


def _book() -> Gw:
    pi, ti = ltp("INFY")
    pt, tt = ltp("TCS")
    pc, tc = ltp("ICICIBANK")
    ps, ts = ltp("SBIN")
    return Gw(
        holdings=[holding("INFY", 100, 182040), holding("TCS", 20, pt + pt // 10)],  # TCS is about 9% below its average
        positions=[position("ICICIBANK", "MIS", 40, pc + pc // 10), position("SBIN", "MIS", 10, ps - 10 * ts)],
    )


def test_what_did_i_pay_on_average_for_infy():
    text, cards = _ask("What did I pay on average for INFY?", _book())
    assert inr(182040) in text and "INFY" in text and "100 shares" in text
    assert [c["kind"] for c in cards] == ["holdings"] and cards[0]["title"] == "Holding: INFY" and len(cards[0]["data"]) == 1


def test_average_price_falls_back_to_an_open_position():
    text, cards = _ask("What did I pay on average for ICICIBANK?", _book())
    assert "ICICIBANK MIS position" in text
    assert [c["kind"] for c in cards] == ["holdings", "positions"]


def test_which_positions_are_down_more_than_5_percent_covers_positions_and_holdings():
    text, cards = _ask("Which positions are down more than 5%?", _book())
    kinds = {c["kind"]: c for c in cards}
    assert set(kinds) == {"holdings", "positions"}
    assert [h["instrument"]["symbol"] for h in kinds["holdings"]["data"]] == ["TCS"]
    assert [p["instrument"]["symbol"] for p in kinds["positions"]["data"]] == ["ICICIBANK"]
    assert "TCS" in text and "ICICIBANK" in text and "from your average price" in text


def test_holdings_down_today_uses_the_previous_close():
    i = inst("INFY")
    four_pct = (i.prev_close_paise * 4 // 100) // i.tick_size_paise
    pi, _ = ltp("INFY", offset_ticks=-four_pct)
    pt, _ = ltp("TCS")
    gw = Gw(holdings=[holding("INFY", 100, pi - 5000), holding("TCS", 20, pt + pt // 10)])  # INFY up from average, down today
    text, cards = _ask("Which holdings are down 3% today?", gw)
    assert [c["kind"] for c in cards] == ["holdings"]
    assert [h["instrument"]["symbol"] for h in cards[0]["data"]] == ["INFY"]
    assert "INFY" in text and "TCS" not in text and "today" in text


def test_nifty_options_near_the_money_for_this_expiry():
    ltp("INFY")
    text, cards = _ask("Show NIFTY options near the money for this expiry", Gw())
    assert [c["kind"] for c in cards] == ["option_chain"]
    oc = cards[0]["data"]
    assert oc["expiry"] == oc["expiries"][0] and any(r["is_atm"] for r in oc["rows"])
    assert "at-the-money strike" in text


def test_pnl_today_routes_to_the_account_summary():
    text, cards = _ask("What's my P&L today?", _book())
    assert [c["kind"] for c in cards] == ["account_summary"] and "P&L" in text
