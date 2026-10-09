"""Deterministic verifier: lot, tick, freeze, price band, margin, holdings, look-alike, suspended, stale data,
and field provenance (misread guard)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from countersign.core.db import session_scope
from countersign.core.parser import extract_facts
from countersign.core.schemas import OrderIntent, OrderType, Side
from countersign.core.verifier import build_order, verify_order
from tests.conftest import set_tick


def _codes(text, intent, account, engine="deterministic"):
    with session_scope() as s:
        br = build_order(s, intent, extract_facts(text), account, engine=engine)
        assert br.canonical is not None, br.clarify
        issues, preview = verify_order(s, br.canonical, account)
        return {i.code for i in br.issues + issues}, br, preview


def test_happy_path_has_no_blocks_and_full_provenance(account):
    set_tick("NSE:EQ:INFY", 186000)
    codes, br, preview = _codes("buy 10 INFY at 1860", OrderIntent(side=Side.BUY, instrument_ref="INFY", quantity=10, order_type=OrderType.LIMIT, limit_price=Decimal("1860")), account)
    assert not {c for c in codes if c not in ("LIMIT_FAR_ABOVE_MARKET",)} - {"UNTRUSTED_TEXT_REMOVED"}
    src = {f.key: f.source.value for f in br.fields}
    assert src["quantity"] == src["side"] == src["limit_price"] == "trader_instruction"
    assert src["lot_size"] == "instrument_metadata" and src["validity"] == "system_default"
    assert preview.band_low_paise < 186000 < preview.band_high_paise


def test_misread_quantity_is_blocked(account):
    set_tick("NSE:EQ:INFY", 186000)
    codes, br, _ = _codes("buy 100 INFY at 1860", OrderIntent(side=Side.BUY, instrument_ref="INFY", quantity=1000, order_type=OrderType.LIMIT, limit_price=Decimal("1860")), account, engine="ollama:test")
    assert "QUANTITY_MISMATCH" in codes
    assert br.disagreements[0].trader_value == "100" and br.disagreements[0].model_value == "1000"


def test_invented_values_are_model_inferred_and_blocked(account):
    set_tick("NSE:EQ:INFY", 186000)
    codes, _, _ = _codes("how is infy doing", OrderIntent(side=Side.BUY, instrument_ref="INFY", quantity=5000, order_type=OrderType.MARKET), account, engine="ollama:test")
    assert {"SIDE_NOT_STATED", "QUANTITY_NOT_STATED", "ORDER_TYPE_NOT_STATED"} <= codes


def test_tick_size_and_price_band(account):
    set_tick("NSE:EQ:INFY", 186000)
    codes, _, _ = _codes("buy 10 INFY at 1860.03", OrderIntent(side=Side.BUY, instrument_ref="INFY", quantity=10, order_type=OrderType.LIMIT, limit_price=Decimal("1860.03")), account)
    assert "TICK_SIZE" in codes
    codes, _, _ = _codes("buy 10 INFY at 2500", OrderIntent(side=Side.BUY, instrument_ref="INFY", quantity=10, order_type=OrderType.LIMIT, limit_price=Decimal("2500")), account)
    assert "PRICE_BAND" in codes


def test_lot_size_and_freeze_quantity_on_options(account):
    from countersign.core import instruments

    exp = instruments.nifty_expiries()[0]
    with session_scope() as s:
        from countersign.core.models import Instrument

        opt = s.get(Instrument, instruments.option_id(exp, 25400, "CE"))
    assert opt is not None
    set_tick(opt.instrument_id, 12000)
    codes, _, _ = _codes("buy 100 NIFTY 25400 CE at 120", OrderIntent(side=Side.BUY, instrument_ref=opt.instrument_id, quantity=100, order_type=OrderType.LIMIT, limit_price=Decimal("120")), account)
    assert "LOT_SIZE" in codes
    codes, _, _ = _codes("buy 3575 NIFTY 25400 CE at 120", OrderIntent(side=Side.BUY, instrument_ref=opt.instrument_id, quantity=3575, order_type=OrderType.LIMIT, limit_price=Decimal("120")), account)
    assert "FREEZE_QUANTITY" in codes


def test_margin_and_holdings(account):
    set_tick("NSE:EQ:TCS", 410000)
    set_tick("NSE:EQ:INFY", 186000)
    codes, _, _ = _codes("buy 100000 TCS at 4100", OrderIntent(side=Side.BUY, instrument_ref="TCS", quantity=100000, order_type=OrderType.LIMIT, limit_price=Decimal("4100")), account)
    assert "INSUFFICIENT_MARGIN" in codes
    codes, _, _ = _codes("sell 500 INFY at 1860", OrderIntent(side=Side.SELL, instrument_ref="INFY", quantity=500, order_type=OrderType.LIMIT, limit_price=Decimal("1860")), account)
    assert "INSUFFICIENT_HOLDINGS" in codes


def test_lookalike_and_suspended_and_stale(account):
    with session_scope() as s:
        br = build_order(s, OrderIntent(side=Side.BUY, instrument_ref="RΕLIANCE", quantity=1, order_type=OrderType.LIMIT, limit_price=Decimal("2800")), extract_facts("buy 1 RΕLIANCE at 2800"), account, engine="deterministic")
        assert br.canonical is None and br.clarify is not None and "look-alike" in br.clarify[0]
    set_tick("NSE:EQ:XYZSUSP", 12000)
    codes, _, _ = _codes("buy 1 XYZSUSP at 120", OrderIntent(side=Side.BUY, instrument_ref="XYZSUSP", quantity=1, order_type=OrderType.LIMIT, limit_price=Decimal("120")), account)
    assert "INSTRUMENT_NOT_TRADABLE" in codes
    set_tick("NSE:EQ:INFY", 186000, ts=datetime.now(UTC) - timedelta(seconds=30))
    codes, _, _ = _codes("buy 1 INFY at 1860", OrderIntent(side=Side.BUY, instrument_ref="INFY", quantity=1, order_type=OrderType.LIMIT, limit_price=Decimal("1860")), account)
    assert "STALE_MARKET_DATA" in codes


def test_market_orders_always_carry_protection(account):
    set_tick("NSE:EQ:INFY", 186000)
    with session_scope() as s:
        br = build_order(s, OrderIntent(side=Side.BUY, instrument_ref="INFY", quantity=1, order_type=OrderType.MARKET), extract_facts("buy 1 INFY at market"), account, engine="deterministic")
    assert br.canonical.protection_price_paise is not None and br.canonical.protection_price_paise > 186000
