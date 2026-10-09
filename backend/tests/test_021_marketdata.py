"""Market socket -> ticks: the median filter over the interleaved 021 price series, re-stamping only while the
connection is provably alive, and the Attack Lab override. isolated=True: these tests write tick keys to the test
Valkey database and never publish on a (cross-database) channel."""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path

from countersign.broker021.marketdata import Conn, MarketData
from countersign.core import events

FIX = Path(__file__).parent / "fixtures" / "021"
KEY = (1, 1594)
INFO = {"instrument_id": "NSE:EQ:INFY", "symbol": "INFY", "segment": "EQ", "prev_close": 99200}


class _Gw:
    _book: dict = {}


def _md(median: int = 5) -> MarketData:
    md = MarketData(_Gw(), "wss://example.invalid", publish_ticks=True, isolated=True, median=median)
    md.want = {KEY: INFO}
    md._save_close = lambda *a, **k: None  # never touch the instrument master from these tests
    return md


def _ltp(token: int, price: int) -> bytes:
    return struct.pack(">HHii", 1, 1, token, price)


def _tick() -> dict:
    return json.loads(events.client().get(events.TICK_KEY.format("NSE:EQ:INFY")))


def test_isolated_spikes_from_the_minority_series_do_not_move_the_published_price():
    md = _md()
    for p in (99100, 99200, 101700, 99150, 99180, 101720, 99210):
        md.on_frame(_ltp(1594, p))
    assert _tick()["ltp_paise"] in (99150, 99180, 99200, 99210)
    assert md.suppressed >= 1 and md.last[KEY]["raw_ltp"] == 99210


def test_a_sustained_move_passes_through_within_three_updates():
    md = _md()
    for p in (99100, 99120, 99140, 99160, 99180):
        md.on_frame(_ltp(1594, p))
    for p in (102000, 102010, 102020):
        md.on_frame(_ltp(1594, p))
    assert _tick()["ltp_paise"] >= 102000


def test_the_live_dual_series_capture_publishes_the_dominant_series():
    md = _md()
    for h in json.loads((FIX / "ws_market_full_dual_series.json").read_text()):
        md.on_frame(bytes.fromhex(h))
    assert 98500 < _tick()["ltp_paise"] < 100000


def test_unchanged_instruments_are_restamped_only_while_the_socket_is_alive():
    md = _md(median=1)
    md.on_frame(_ltp(1594, 99100))
    first = _tick()
    conn = Conn(md, 0)
    conn.subscribed = {KEY}
    md.conns = [conn]
    md.published_at[KEY] = 0
    md.restamp()
    assert _tick()["seq"] == first["seq"]  # no socket: nothing re-stamped, the tick will go stale
    conn.ws = object()
    conn.alive_at = time.time()
    md.restamp()
    assert _tick()["seq"] == first["seq"] + 1 and _tick()["ltp_paise"] == 99100


def test_attack_lab_override_keeps_the_gateway_off_an_instrument():
    md = _md(median=1)
    events.client().set("cs:feed:override:NSE:EQ:INFY", "1", ex=60)
    md.refresh_overrides()
    md.on_frame(_ltp(1594, 99100))
    assert events.client().get(events.TICK_KEY.format("NSE:EQ:INFY")) is None
    events.client().delete("cs:feed:override:NSE:EQ:INFY")
    md.refresh_overrides()
    md.on_frame(_ltp(1594, 99100))
    assert _tick()["ltp_paise"] == 99100
