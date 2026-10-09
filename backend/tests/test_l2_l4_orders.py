"""L2 orders end to end through the real proposal service: market orders with an honest worst case, sells of a
fraction of a holding, stop-loss orders under 021's rules, and moving a stop-loss (MODIFY with the full new state)."""

from decimal import Decimal

from countersign.core.money import inr
from countersign.core.parser import parse
from countersign.core.schemas import OrderIntent, OrderType, Product, Side
from countersign.core.verifier import protection_price
from countersign.llm import tools as T
from tests.test_l2_l4_support import (
    Gw,
    draft_order,
    fields,
    holding,
    ltp,
    open_order,
    position,
    proposal_count,
    rupees,
    user_id,
    view,
)


def _ok(out) -> dict:
    assert out.proposal is not None, out.clarify or out.notice
    return view(out.proposal.id)


def _blocking(v: dict) -> set[str]:
    return {i["code"] for i in v["issues"] if i["severity"] == "BLOCK"}


# ---------------------------------------------------------------- 1. "Buy 10 Reliance at market"


def test_buy_at_market_resolves_reliance_and_shows_the_worst_case():
    p, t = ltp("RELIANCE")
    v = _ok(draft_order("Buy 10 Reliance at market", Gw()))
    c = v["steps"][0]["canonical"]
    assert v["status"] == "AWAITING_APPROVAL" and not _blocking(v)
    assert (c["side"], c["quantity"], c["order_type"], c["instrument"]["symbol"]) == ("BUY", 10, "MARKET", "RELIANCE")
    cap = protection_price(Side.BUY, p, t)
    assert c["protection_price_paise"] == cap and cap > p and cap % t == 0 and c["limit_price_paise"] is None
    f = fields(v)
    assert f["order_type"]["source"] == "trader_instruction" and f["quantity"]["source"] == "trader_instruction"
    assert f["protection_price"]["source"] == "risk_engine"
    assert inr(cap * 10) in f["protection_price"]["note"] and "never above" in f["protection_price"]["note"]
    pv = v["steps"][0]["preview"]
    assert pv["worst_case_price_paise"] == cap and pv["order_value_paise"] == cap * 10
    assert "protected at" in v["summary"]


def test_market_order_the_trader_did_not_ask_for_is_blocked():
    ltp("RELIANCE")
    intent = OrderIntent(side=Side.BUY, instrument_ref="RELIANCE", quantity=10, order_type=OrderType.MARKET)
    v = _ok(draft_order("Buy 10 Reliance at 1200", Gw(), intent=intent, engine="groq:test"))
    assert "ORDER_TYPE_NOT_STATED" in _blocking(v)


def test_market_order_on_a_lookalike_symbol_is_never_drafted_as_valid():
    ltp("RELIANCE")
    out = draft_order("Buy 10 RΕLIANCE at market", Gw())
    assert out.proposal is None or "LOOKALIKE_SYMBOL" in _blocking(view(out.proposal.id))


# ---------------------------------------------------------------- 2. "Sell half my TCS" (and all / 25% / a third)


def _tcs(qty=25, **extra):
    p, t = ltp("TCS")
    return Gw(holdings=[holding("TCS", qty, p - 50 * t, **extra)]), p, t


def test_sell_half_is_computed_from_the_holding_and_rounded_down():
    gw, _, _ = _tcs(25)
    v = _ok(draft_order("Sell half my TCS at market", gw))
    c = v["steps"][0]["canonical"]
    assert v["status"] == "AWAITING_APPROVAL" and not _blocking(v)
    assert (c["side"], c["quantity"], c["product"], c["order_type"]) == ("SELL", 12, "CNC", "MARKET")
    f = fields(v)
    assert f["quantity"]["source"] == "account" and not f["quantity"]["flagged"]
    assert f["quantity"]["note"] == "Computed from your holdings: half of 25 shares = 12.5, rounded down to 12 whole shares"
    assert "25 held" in f["quantity_basis"]["display"]
    assert f["product"]["source"] == "account"


def test_sell_all_quarter_and_third():
    for text, qty, arithmetic in (
        ("Sell all my TCS at market", 25, "all of 25 shares = 25"),
        ("Sell 25% of my TCS at market", 6, "25% of 25 shares = 6.25, rounded down to 6 whole shares"),
        ("Sell a third of my TCS at market", 8, "a third of 25 shares = 8.3333, rounded down to 8 whole shares"),
    ):
        gw, _, _ = _tcs(25)
        v = _ok(draft_order(text, gw))
        assert v["steps"][0]["canonical"]["quantity"] == qty, text
        assert fields(v)["quantity"]["note"].endswith(arithmetic), fields(v)["quantity"]["note"]


def test_pending_sells_reduce_what_can_be_sold():
    p, t = ltp("TCS")
    gw = Gw(holdings=[holding("TCS", 25, p)], orders=[open_order("900001", "TCS", "SELL", 5, "LIMIT", limit=p + 40 * t)])
    v = _ok(draft_order("Sell half my TCS at market", gw))
    assert v["steps"][0]["canonical"]["quantity"] == 10
    assert "25 held - 5 in pending sell orders = 20 sellable" in fields(v)["quantity_basis"]["display"]


def test_021_holdings_use_settled_plus_unsettled_minus_already_sold():
    gw, _, _ = _tcs(25, free_quantity=20, btst_quantity=5, sell_quantity=4)
    v = _ok(draft_order("Sell half my TCS at market", gw))
    assert v["steps"][0]["canonical"]["quantity"] == 10
    assert "20 settled + 5 unsettled (T1) - 4 already sold or in pending sell orders = 21 sellable" in fields(v)["quantity_basis"]["display"]


def test_zero_or_nothing_held_is_explained_and_never_drafted():
    gw, _, _ = _tcs(1)
    out = draft_order("Sell half my TCS at market", gw)
    assert out.proposal is None and "rounds down to 0 whole shares" in out.clarify[0] and "No order was drafted" in out.clarify[0]
    ltp("TCS")
    out = draft_order("Sell half my TCS at market", Gw())
    assert out.proposal is None and "no TCS to sell" in out.clarify[0]
    assert proposal_count() == 0


def test_fraction_of_an_intraday_position_uses_the_position():
    p, _ = ltp("ICICIBANK")
    gw = Gw(positions=[position("ICICIBANK", "MIS", 30, p)])
    v = _ok(draft_order("Sell half my ICICIBANK position at market", gw))
    c = v["steps"][0]["canonical"]
    assert (c["quantity"], c["product"]) == (15, "MIS")
    assert "MIS position: long 30" in fields(v)["quantity_basis"]["display"]


def test_holding_and_position_together_ask_which_one():
    p, _ = ltp("TCS")
    gw = Gw(holdings=[holding("TCS", 25, p)], positions=[position("TCS", "MIS", 10, p)])
    out = draft_order("Sell half my TCS at market", gw)
    assert out.proposal is None and len(out.clarify[1]) == 2
    v = _ok(draft_order(out.clarify[1][0], gw))
    assert (v["steps"][0]["canonical"]["quantity"], v["steps"][0]["canonical"]["product"]) == (12, "CNC")
    v = _ok(draft_order(out.clarify[1][1], gw))
    assert (v["steps"][0]["canonical"]["quantity"], v["steps"][0]["canonical"]["product"]) == (5, "MIS")


def _ai(text: str, gw: Gw, **kw) -> dict:
    cards: list[dict] = []
    d = T.ChatDeps(user_id=user_id(), account_id="LAB1", conversation_id="c-l2", user_text=text, engine="groq:test", emit=cards.append, origin="test", gateway=gw)
    return T.propose_order(d, **kw)


def test_ai_fraction_path_and_misread_guard():
    gw, _, _ = _tcs(25)
    r = _ai("Sell half my TCS at market", gw, side="SELL", symbol="TCS", fraction="1/2", order_type="MARKET")
    assert r["status"] == "AWAITING_APPROVAL", r
    assert view(r["proposal_id"])["steps"][0]["canonical"]["quantity"] == 12
    r = _ai("Sell half my TCS at market", gw, side="SELL", symbol="TCS", quantity=12, order_type="MARKET")
    assert r["status"] == "AWAITING_APPROVAL", r
    r = _ai("Sell half my TCS at market", gw, side="SELL", symbol="TCS", quantity=13, order_type="MARKET")
    v = view(r["proposal_id"])
    assert "QUANTITY_MISMATCH" in _blocking(v) and v["interpretation"]["disagreements"][0]["model_value"] == "13"
    r = _ai("Sell half my TCS at market", gw, side="SELL", symbol="TCS", fraction="1/3", order_type="MARKET")
    assert "FRACTION_MISMATCH" in _blocking(view(r["proposal_id"]))
    r = _ai("Sell 25% of my TCS at market", gw, side="SELL", symbol="TCS", quantity=25, order_type="MARKET")
    assert "QUANTITY_MISMATCH" in _blocking(view(r["proposal_id"]))


def test_ai_product_never_decides_holding_or_position():
    p, _ = ltp("TCS")
    both = Gw(holdings=[holding("TCS", 25, p)], positions=[position("TCS", "MIS", 10, p)])
    r = _ai("Sell half my TCS at market", both, side="SELL", symbol="TCS", fraction="1/2", order_type="MARKET", product="MIS")
    assert r["status"] == "NEEDS_CLARIFICATION"
    pos_only = Gw(positions=[position("TCS", "MIS", 10, p)])
    r = _ai("Sell half my TCS at market", pos_only, side="SELL", symbol="TCS", fraction="1/2", order_type="MARKET", product="CNC")
    assert "PRODUCT_MISMATCH" in _blocking(view(r["proposal_id"]))


def test_quantities_never_stated_or_implied_are_still_blocked():
    gw, _, _ = _tcs(25)
    r = _ai("Sell my TCS at market", gw, side="SELL", symbol="TCS", fraction="1/2", order_type="MARKET")
    assert "FRACTION_NOT_STATED" in _blocking(view(r["proposal_id"]))
    r = _ai("Sell my TCS at market", gw, side="SELL", symbol="TCS", quantity=10, order_type="MARKET")
    assert "QUANTITY_NOT_STATED" in _blocking(view(r["proposal_id"]))


# ---------------------------------------------------------------- 3. stop-loss orders (021 rules)


def test_sell_stop_loss_valid_and_its_card():
    p, t = ltp("INFY")
    trig, lim = p - 20 * t, p - 22 * t
    gw = Gw(holdings=[holding("INFY", 100, p)])
    v = _ok(draft_order(f"Sell 10 INFY SL trigger {rupees(trig)} limit {rupees(lim)}", gw))
    c = v["steps"][0]["canonical"]
    assert not _blocking(v), v["issues"]
    assert (c["order_type"], c["trigger_price_paise"], c["limit_price_paise"]) == ("SL", trig, lim)
    f = fields(v)
    assert f["trigger_price"]["source"] == "trader_instruction" and f["limit_price"]["source"] == "trader_instruction"
    assert v["steps"][0]["preview"]["worst_case_price_paise"] == lim
    assert f"SL trigger {inr(trig)} limit {inr(lim)}" in v["summary"]


def test_stop_loss_rules_block_and_explain():
    p, t = ltp("INFY")
    gw = Gw(holdings=[holding("INFY", 100, p)])
    cases = [
        (f"Sell 10 INFY SL trigger {rupees(p + 10 * t)} limit {rupees(p + 8 * t)}", "SL_TRIGGER_SIDE"),
        (f"Sell 10 INFY SL trigger {rupees(p - 20 * t)} limit {rupees(p - 18 * t)}", "SL_LIMIT_SIDE"),
        (f"Buy 10 INFY SL trigger {rupees(p - 10 * t)} limit {rupees(p - 8 * t)}", "SL_TRIGGER_SIDE"),
        (f"Buy 10 INFY SL trigger {rupees(p + 20 * t)} limit {rupees(p + 18 * t)}", "SL_LIMIT_SIDE"),
    ]
    for text, code in cases:
        v = _ok(draft_order(text, gw))
        assert code in _blocking(v), (text, v["issues"])
    v = _ok(draft_order(f"Buy 10 INFY SL trigger {rupees(p + 20 * t)} limit {rupees(p + 22 * t)}", gw))
    assert not _blocking(v), v["issues"]


def test_slm_is_capped_from_the_trigger():
    p, t = ltp("INFY")
    trig = p - 20 * t
    v = _ok(draft_order(f"sell 10 INFY SL-M trigger {rupees(trig)}", Gw(holdings=[holding("INFY", 100, p)])))
    c = v["steps"][0]["canonical"]
    assert c["order_type"] == "SL-M" and c["limit_price_paise"] is None
    assert c["protection_price_paise"] == protection_price(Side.SELL, trig, t) < trig
    assert not _blocking(v)


def test_off_tick_trigger_is_blocked_not_rounded():
    p, t = ltp("RELIANCE")
    if t < 2:
        return
    trig = p - 20 * t + 1
    v = _ok(draft_order(f"sell 10 RELIANCE SL-M trigger {rupees(trig)}", Gw(holdings=[holding("RELIANCE", 50, p)])))
    assert v["steps"][0]["canonical"]["trigger_price_paise"] == trig
    assert any(i["code"] == "TICK_SIZE" and i["field"] == "trigger_price" for i in v["issues"])


def test_ai_misreads_of_stop_loss_orders_are_blocked():
    p, t = ltp("INFY")
    gw = Gw(holdings=[holding("INFY", 100, p)])
    trig = p - 20 * t
    text = f"Sell 10 INFY stop loss at {rupees(trig)}"
    as_limit = OrderIntent(side=Side.SELL, instrument_ref="INFY", quantity=10, order_type=OrderType.LIMIT, limit_price=Decimal(rupees(trig)))
    assert "ORDER_TYPE_MISMATCH" in _blocking(_ok(draft_order(text, gw, intent=as_limit, engine="groq:test")))
    reused = OrderIntent(side=Side.SELL, instrument_ref="INFY", quantity=10, order_type=OrderType.SL, limit_price=Decimal(rupees(trig)), trigger_price=Decimal(rupees(trig)))
    assert "PRICE_NOT_STATED" in _blocking(_ok(draft_order(text, gw, intent=reused, engine="groq:test")))
    slm = OrderIntent(side=Side.SELL, instrument_ref="INFY", quantity=10, order_type=OrderType.SLM, trigger_price=Decimal(rupees(trig)))
    assert "ORDER_TYPE_NOT_STATED" in _blocking(_ok(draft_order(text, gw, intent=slm, engine="groq:test")))
    other = OrderIntent(side=Side.SELL, instrument_ref="INFY", quantity=10, order_type=OrderType.SL, limit_price=Decimal(rupees(trig - t)), trigger_price=Decimal(rupees(trig - 2 * t)))
    text2 = f"Sell 10 INFY SL trigger {rupees(trig)} limit {rupees(trig - t)}"
    assert "TRIGGER_MISMATCH" in _blocking(_ok(draft_order(text2, gw, intent=other, engine="groq:test")))
    sneaky = OrderIntent(side=Side.SELL, instrument_ref="INFY", quantity=10, order_type=OrderType.SL, limit_price=Decimal(rupees(trig - t)), trigger_price=Decimal(rupees(trig)))
    assert "ORDER_TYPE_NOT_STATED" in _blocking(_ok(draft_order(f"Sell 10 INFY at {rupees(trig - t)}", gw, intent=sneaky, engine="groq:test")))
    with_trigger = OrderIntent(side=Side.SELL, instrument_ref="INFY", quantity=10, order_type=OrderType.LIMIT, limit_price=Decimal(rupees(p)), trigger_price=Decimal(rupees(trig)))
    assert "TRIGGER_NOT_ALLOWED" in _blocking(_ok(draft_order(f"Sell 10 INFY at {rupees(p)}", gw, intent=with_trigger, engine="groq:test")))


# ---------------------------------------------------------------- 4. "Move my stop-loss on HDFC Bank up to 1640"


def _hdfc(*orders):
    p, t = ltp("HDFCBANK")
    return Gw(holdings=[holding("HDFCBANK", 60, p)], orders=list(orders)), p, t


def test_move_stop_loss_builds_a_full_state_modify_keeping_the_gap():
    p, t = ltp("HDFCBANK")
    old_trig, old_lim = p - 40 * t, p - 42 * t
    gw, _, _ = _hdfc(open_order("777001", "HDFCBANK", "SELL", 10, "SL", trigger=old_trig, limit=old_lim))
    new = p - 20 * t
    v = _ok(draft_order(f"Move my stop-loss on HDFC Bank up to {rupees(new)}", gw))
    c = v["steps"][0]["canonical"]
    assert v["status"] == "AWAITING_APPROVAL" and not _blocking(v), v["issues"]
    assert c["action"] == "MODIFY" and c["target_broker_order_id"] == "777001"
    assert (c["quantity"], c["order_type"], c["product"], c["validity"]) == (10, "SL", "CNC", "DAY")
    assert (c["trigger_price_paise"], c["limit_price_paise"]) == (new, new - 2 * t)
    assert c["modify_from"] == {"quantity": 10, "order_type": "SL", "limit_price_paise": old_lim, "trigger_price_paise": old_trig}
    f = fields(v)
    assert f["trigger_price"]["display"] == f"{inr(old_trig)} → {inr(new)}" and f["trigger_price"]["source"] == "trader_instruction"
    assert f["limit_price"]["display"] == f"{inr(old_lim)} → {inr(new - 2 * t)}" and "gap" in f["limit_price"]["note"]
    assert "→" in v["summary"]


def test_move_stop_loss_with_a_stated_limit_and_for_slm():
    p, t = ltp("HDFCBANK")
    gw, _, _ = _hdfc(open_order("777001", "HDFCBANK", "SELL", 10, "SL", trigger=p - 40 * t, limit=p - 42 * t))
    v = _ok(draft_order(f"Move my stop-loss on HDFC Bank up to {rupees(p - 20 * t)} limit {rupees(p - 25 * t)}", gw))
    assert v["steps"][0]["canonical"]["limit_price_paise"] == p - 25 * t and fields(v)["limit_price"]["source"] == "trader_instruction"
    gw, _, _ = _hdfc(open_order("777002", "HDFCBANK", "SELL", 10, "SL-M", trigger=p - 40 * t))
    v = _ok(draft_order(f"Move my stop-loss on HDFC Bank up to {rupees(p - 20 * t)}", gw))
    c = v["steps"][0]["canonical"]
    assert c["limit_price_paise"] is None and c["protection_price_paise"] == protection_price(Side.SELL, p - 20 * t, t)


def test_move_stop_loss_guards():
    p, t = ltp("HDFCBANK")
    sl = open_order("777001", "HDFCBANK", "SELL", 10, "SL", trigger=p - 40 * t, limit=p - 42 * t)
    gw, _, _ = _hdfc(sl)
    v = _ok(draft_order(f"Move my stop-loss on HDFC Bank down to {rupees(p - 20 * t)}", gw))
    assert "DIRECTION_MISMATCH" in _blocking(v)
    v = _ok(draft_order(f"Move my stop-loss on HDFC Bank up to {rupees(p + 10 * t)}", gw))
    assert "SL_TRIGGER_SIDE" in _blocking(v)
    v = _ok(draft_order(f"Move my stop-loss on HDFC Bank to {rupees(p - 40 * t)}", gw))
    assert "NO_CHANGE" in _blocking(v)
    two, _, _ = _hdfc(sl, open_order("777009", "HDFCBANK", "SELL", 5, "SL", trigger=p - 30 * t, limit=p - 31 * t))
    out = draft_order(f"Move my stop-loss on HDFC Bank up to {rupees(p - 20 * t)}", two)
    assert out.proposal is None and "2 open stop-loss orders" in out.clarify[0]
    pick = parse(out.clarify[1][1])
    assert pick.kind == "modify" and pick.order.target_order_ref == "777009"
    v = _ok(draft_order(out.clarify[1][1], two))
    assert v["steps"][0]["canonical"]["target_broker_order_id"] == "777009" and not _blocking(v)
    lim_only, _, _ = _hdfc(open_order("777003", "HDFCBANK", "SELL", 10, "LIMIT", limit=p + 20 * t, status="OPEN"))
    out = draft_order(f"Move my stop-loss on HDFC Bank up to {rupees(p - 20 * t)}", lim_only)
    assert out.proposal is None and "no open stop-loss order on HDFCBANK" in out.clarify[0] and "777003" in out.clarify[0]
    out = draft_order(f"modify my HDFCBANK order to {rupees(p - 20 * t)}", gw)
    assert out.proposal is None and "stop-loss" in out.clarify[0]


def test_ai_cannot_move_a_stop_loss_the_trader_did_not_name():
    p, t = ltp("HDFCBANK")
    q, u = ltp("INFY")
    gw = Gw(
        holdings=[holding("HDFCBANK", 60, p), holding("INFY", 100, q)],
        orders=[open_order("777001", "HDFCBANK", "SELL", 10, "SL", trigger=p - 40 * t, limit=p - 42 * t), open_order("888001", "INFY", "SELL", 10, "SL", trigger=q - 40 * u, limit=q - 42 * u)],
    )
    cards: list[dict] = []
    d = T.ChatDeps(user_id=user_id(), account_id="LAB1", conversation_id="c-sl", user_text=f"Move my stop-loss on HDFC Bank up to {rupees(p - 20 * t)}", engine="groq:test", emit=cards.append, origin="test", gateway=gw)
    r = T.propose_modify_stop_loss(d, float(rupees(q - 20 * u)), symbol="INFY")
    assert {"INSTRUMENT_NOT_STATED", "TRIGGER_MISMATCH"} <= _blocking(view(r["proposal_id"]))
    d3 = T.ChatDeps(user_id=user_id(), account_id="LAB1", conversation_id="c-sl3", user_text="Raise my stop-loss on HDFC Bank a little", engine="groq:test", emit=cards.append, origin="test", gateway=gw)
    r = T.propose_modify_stop_loss(d3, float(rupees(p - 20 * t)), symbol="HDFCBANK")
    assert "TRIGGER_NOT_STATED" in _blocking(view(r["proposal_id"]))
    d2 = T.ChatDeps(user_id=user_id(), account_id="LAB1", conversation_id="c-sl2", user_text="Move my stop-loss on RΕLIANCE up to 1640", engine="groq:test", emit=cards.append, origin="test", gateway=gw)
    r = T.propose_modify_stop_loss(d2, 1640.0, symbol="RΕLIANCE")
    assert r["status"] == "NEEDS_CLARIFICATION" and "look-alike" in r["question"]


def test_modify_canonical_hash_covers_the_old_and_new_state():
    from countersign.core.canonical import canonical_hash
    from countersign.core.schemas import CanonicalOrder

    p, t = ltp("HDFCBANK")
    gw, _, _ = _hdfc(open_order("777001", "HDFCBANK", "SELL", 10, "SL", trigger=p - 40 * t, limit=p - 42 * t))
    v = _ok(draft_order(f"Move my stop-loss on HDFC Bank up to {rupees(p - 20 * t)}", gw))
    c = CanonicalOrder.model_validate(v["steps"][0]["canonical"])
    assert c.product == Product.CNC and canonical_hash(c) == canonical_hash(v["steps"][0]["canonical"])
    tampered = c.model_copy(update={"trigger_price_paise": c.trigger_price_paise + t})
    assert canonical_hash(tampered) != canonical_hash(c)
