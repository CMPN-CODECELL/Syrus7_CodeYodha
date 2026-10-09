"""Parser routing and stated facts for the official 021-01 examples (L1 to L4), fractions, stop-loss phrasing and
the request detectors the AI tool guards rely on."""

from decimal import Decimal
from fractions import Fraction

import pytest

from countersign.core.parser import detect_exit_losers, detect_holdings_alert, detect_rebalance, extract_facts, parse, parse_fraction


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Buy 10 Reliance at market", "order"),
        ("Sell half my TCS at market", "order"),
        ("Sell half my TCS", "clarify"),
        ("sell all my INFY at 1850", "order"),
        ("Sell 25% of my TCS at market", "order"),
        ("Sell a third of ITC at market", "order"),
        ("Sell 10 INFY SL trigger 1800 limit 1795", "order"),
        ("sell 10 INFY SL-M trigger 1800", "order"),
        ("Sell 10 INFY stop loss at 1800", "clarify"),
        ("Move my stop-loss on HDFC Bank up to 1640", "modify"),
        ("If Reliance drops below 2800, buy 10", "mandate"),
        ("If Reliance goes below 2800, buy 10", "mandate"),
        ("If Reliance falls under 2800, buy 10", "mandate"),
        ("Tell me when any holding falls 3% in a day", "mandate"),
        ("Exit all my losing intraday positions", "exit_losers"),
        ("Rebalance so no single stock is more than 20% of my portfolio", "rebalance"),
        ("What did I pay on average for INFY?", "query"),
        ("Which positions are down more than 5%?", "query"),
        ("Show NIFTY options near the money for this expiry", "query"),
        ("What's my P&L today?", "query"),
        ("sell most of my TCS", "clarify"),
        ("Buy half my INFY at market", "clarify"),
        ("Rebalance my portfolio", "clarify"),
    ],
)
def test_official_phrasing_routes(text, kind):
    assert parse(text).kind == kind


def test_buy_at_market_is_a_market_intent_with_the_stated_quantity():
    o = parse("Buy 10 Reliance at market").order
    assert o.side.value == "BUY" and o.quantity == 10 and o.order_type.value == "MARKET" and o.limit_price is None
    assert o.instrument_ref.lower() == "reliance"


@pytest.mark.parametrize(
    "text,frac",
    [
        ("Sell half my TCS at market", "1/2"),
        ("sell half of my TCS at market", "1/2"),
        ("Sell all my TCS at market", "1/1"),
        ("sell my entire TCS holding at market", "1/1"),
        ("Sell 25% of my TCS at market", "1/4"),
        ("sell 25 percent of TCS at market", "1/4"),
        ("Sell a third of my TCS at market", "1/3"),
        ("sell one quarter of TCS at market", "1/4"),
        ("sell two thirds of TCS at market", "2/3"),
    ],
)
def test_fraction_words_are_stated_quantity_rules_not_quantities(text, frac):
    p = parse(text)
    assert p.kind == "order" and p.order.quantity is None and p.order.quantity_fraction == frac
    assert p.facts.quantity is None and not p.facts.rough
    assert p.order.instrument_ref.upper() == "TCS"


def test_percent_is_never_read_as_a_share_count():
    f = extract_facts("Sell 25% of my TCS at market")
    assert f.quantity is None and f.fraction == Fraction(1, 4)


def test_clarify_options_are_complete_requests_that_parse_on_their_own():
    p = parse("Sell half my TCS")
    assert p.options == ["Sell half my TCS at market"]
    assert parse(p.options[0]).kind == "order"
    q = parse("Sell 10 INFY stop loss at 1800")
    assert q.options and parse(q.options[0]).order.order_type.value == "SL-M"
    assert parse(q.options[0]).order.trigger_price == Decimal("1800")


@pytest.mark.parametrize(
    "raw,expected",
    [("1/2", Fraction(1, 2)), ("0.5", Fraction(1, 2)), ("50%", Fraction(1, 2)), ("half", Fraction(1, 2)), ("all", Fraction(1)), ("1/3", Fraction(1, 3)), ("2", None), ("0", None), ("abc", None)],
)
def test_parse_fraction(raw, expected):
    assert parse_fraction(raw) == expected


def test_stop_loss_trigger_is_never_read_as_the_limit():
    f = extract_facts("Sell 10 INFY stop loss at 1800")
    assert f.stop_loss and f.trigger == Decimal("1800") and f.price is None
    f = extract_facts("Sell 10 INFY SL trigger 1800 limit 1795")
    assert f.trigger == Decimal("1800") and f.price == Decimal("1795") and not f.slm
    f = extract_facts("Sell 10 INFY at 1795 with a stop loss at 1800")
    assert f.trigger == Decimal("1800") and f.price == Decimal("1795")
    f = extract_facts("sell 10 INFY SL-M trigger 1800")
    assert f.slm and f.trigger == Decimal("1800") and f.price is None


def test_stop_loss_orders_parse_to_sl_and_slm():
    sl = parse("Sell 10 INFY SL trigger 1800 limit 1795").order
    assert sl.order_type.value == "SL" and sl.trigger_price == Decimal("1800") and sl.limit_price == Decimal("1795")
    slm = parse("sell 10 INFY SL-M trigger 1800").order
    assert slm.order_type.value == "SL-M" and slm.trigger_price == Decimal("1800") and slm.limit_price is None


@pytest.mark.parametrize("text", ["Move my stop-loss on HDFC Bank up to 1640", "Move my stop-loss to 1640 on HDFC Bank", "raise my SL on HDFC Bank to 1640"])
def test_move_stop_loss_parses_target_and_new_trigger(text):
    p = parse(text)
    assert p.kind == "modify" and p.order.action == "MODIFY"
    assert p.order.trigger_price == Decimal("1640") and p.order.limit_price is None
    assert "HDFC" in (p.order.instrument_ref or "").upper()


def test_move_stop_loss_direction_and_explicit_limit():
    f = extract_facts("Move my stop-loss on HDFC Bank up to 1640")
    assert f.direction == "UP"
    p = parse("Move my stop on INFY down to 1700 limit 1690")
    assert p.facts.direction == "DOWN" and p.order.trigger_price == Decimal("1700") and p.order.limit_price == Decimal("1690")


@pytest.mark.parametrize("text", ["If Reliance drops below 2800, buy 10", "If Reliance goes below 2800, buy 10", "If Reliance falls under 2800, buy 10", "Buy 10 Reliance if it drops below 2800"])
def test_conditional_buy_parses_to_one_shot_limit_mandate(text):
    m = parse(text).mandate
    assert m.kind == "ORDER" and m.op == "LTE" and m.threshold_price == Decimal("2800") and m.scope == "INSTRUMENT"
    assert m.order.side.value == "BUY" and m.order.quantity == 10 and m.order.limit_price == Decimal("2800")
    assert m.instrument_ref.lower() == "reliance"


def test_holdings_alert_is_scoped_to_every_holding_and_day_change():
    m = parse("Tell me when any holding falls 3% in a day").mandate
    assert m.kind == "ALERT" and m.scope == "ALL_HOLDINGS" and m.field == "DAY_CHANGE_BPS" and m.drop_pct == Decimal("3")
    assert m.instrument_ref is None and m.order is None
    p = parse("Tell me when any holding falls 3%")
    assert p.kind == "clarify" and len(p.options) == 2
    assert parse(p.options[0]).mandate.field == "DAY_CHANGE_BPS"
    assert parse(p.options[1]).mandate.field == "DROP_FROM_AVG_BPS"


def test_request_detectors():
    assert detect_exit_losers("Exit all my losing intraday positions") == {"intraday_only": True}
    assert detect_exit_losers("Close my losing positions") == {"intraday_only": False}
    assert detect_exit_losers("Which positions are losing?") is None
    assert detect_exit_losers("Sell 10 INFY at 1850") is None
    assert detect_rebalance("Rebalance so no single stock is more than 20% of my portfolio") == {"max_pct": Decimal("20")}
    assert detect_rebalance("make sure no stock is more than 15% of my holdings") == {"max_pct": Decimal("15")}
    assert detect_rebalance("Buy 10 INFY at market") is None
    assert detect_holdings_alert("Tell me when any holding falls 3% in a day") == {"pct": Decimal("3"), "basis": "DAY"}
    assert detect_holdings_alert("Alert me if INFY falls 3%") is None


def test_l1_queries_route_with_the_right_arguments():
    p = parse("What did I pay on average for INFY?")
    assert p.query == "avg_price" and "INFY" in p.args["symbols"]
    p = parse("Which positions are down more than 5%?")
    assert p.query == "losers" and p.args == {"threshold_pct": 5.0, "scope": "both", "basis": "avg"}
    p = parse("Which holdings are down 3% today?")
    assert p.args["scope"] == "holdings" and p.args["basis"] == "day"
    assert parse("Show NIFTY options near the money for this expiry").query == "option_chain"
