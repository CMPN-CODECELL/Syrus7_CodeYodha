"""Replay feed next to the 021 gateway: it keeps only the fictional instruments, and a demo or Attack Lab scenario on a
real instrument takes it over briefly (cs:feed:override:<id>) starting from the live 021 price; calm hands it back."""

from __future__ import annotations

import json

import pytest

from countersign.core import events
from countersign.core.config import settings
from countersign.feed.main import Feed


@pytest.fixture
def feed021(monkeypatch):
    monkeypatch.setattr(settings(), "gateway021_enabled", True)
    monkeypatch.setattr("countersign.feed.main.publish", lambda ticks: None)
    f = Feed()
    f.load()
    return f


def test_only_fictional_instruments_are_replayed(feed021):
    syms = {s.symbol for s in feed021.syms.values() if not s.external}
    assert {"ABCCORP", "XYZSUSP", "TATAMOTORS"} <= syms
    assert not {"RELIANCE", "INFY", "NIFTY"} & syms
    assert all(s.segment != "FO" for s in feed021.syms.values())
    stepped = {t["symbol"] for t in feed021.step()}
    assert "RELIANCE" not in stepped and "ABCCORP" in stepped


def test_a_scenario_on_a_real_instrument_overrides_it_from_the_live_price(feed021):
    live = {"instrument_id": "NSE:EQ:INFY", "symbol": "INFY", "ltp_paise": 99200, "bid_paise": 99200, "ask_paise": 99200, "volume": 1, "prev_close_paise": 99000, "ts": "2026-10-09T06:00:00Z", "seq": 1, "src": "021"}
    events.client().set(events.TICK_KEY.format("NSE:EQ:INFY"), json.dumps(live))
    feed021.control({"cmd": "scenario", "name": "spike", "instrument_id": "NSE:EQ:INFY"})
    assert events.client().get("cs:feed:override:NSE:EQ:INFY") == "1"
    ticks = {t["instrument_id"]: t for t in feed021.step()}
    assert ticks["NSE:EQ:INFY"]["ltp_paise"] == 99200 * 10300 // 10000 // 5 * 5
    feed021.control({"cmd": "scenario", "name": "calm"})
    assert events.client().get("cs:feed:override:NSE:EQ:INFY") is None
    assert "NSE:EQ:INFY" not in {t["instrument_id"] for t in feed021.step()}
