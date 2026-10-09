"""021 binary websocket parsers against frames captured live from the sandbox (account code replaced by HACK0000)
and synthetic frames built from the layouts in the 021 API guide."""

import json
import struct
from pathlib import Path

from countersign.broker021 import wire
from countersign.broker021.marketdata import tick_payload

FIX = Path(__file__).parent / "fixtures" / "021"


def _frames(name: str) -> list[bytes]:
    return [bytes.fromhex(h) for h in json.loads((FIX / name).read_text()) if not h.startswith(("CONNECTED", "ERROR", "TEXT:"))]


def test_tc1_ltp_frames_hold_several_packets_back_to_back():
    frames = _frames("ws_market_ltp.json")
    assert frames
    for f in frames:
        pk = wire.parse_market_frame(f)
        assert [p["tc"] for p in pk] == [1, 1]
        assert {p["token"] for p in pk} == {1594, 2885} and all(p["exchange"] == "NSECM" for p in pk)
    first = wire.parse_market_frame(frames[0])
    assert first[0]["ltp"] == 99321 and first[1]["ltp"] == 117430


def test_tc1_snapshot_extra_bytes_for_ltpo_and_ltpc():
    # ltpo snapshot packet: 12 bytes + 'o' + open; then an ltpc packet with 'c' + close; then a plain LTP packet.
    a = struct.pack(">HHii", 1, 3, 26000, 2248435) + b"o" + struct.pack(">i", 2230000)
    b = struct.pack(">HHii", 1, 1, 2885, 120700) + b"c" + struct.pack(">i", 120770)
    c = struct.pack(">HHii", 1, 1, 1594, 99200)
    pk = wire.parse_market_frame(a + b + c)
    assert pk[0] == {"tc": 1, "exchange": "NSEIDX", "token": 26000, "ltp": 2248435, "open": 2230000}
    assert pk[1] == {"tc": 1, "exchange": "NSECM", "token": 2885, "ltp": 120700, "close": 120770}
    assert pk[2] == {"tc": 1, "exchange": "NSECM", "token": 1594, "ltp": 99200}


def test_tc3_full_nsecm_from_the_live_capture():
    frames = _frames("ws_market_full.json")
    p = wire.parse_market_frame(frames[0])
    assert len(p) == 1 and p[0]["tc"] == 3 and p[0]["exchange"] == "NSECM" and p[0]["token"] == 2885
    assert p[0]["ltp"] == 120780 and p[0]["close"] == 121800 and p[0]["open"] == 121200 and p[0]["high"] == 122080 and p[0]["low"] == 120350
    assert p[0]["bid"] == 120770 and p[0]["bid_qty"] == 1243 and p[0]["ask"] == 0 and p[0]["volume"] == 11880727


def test_tc3_dual_series_capture_parses_cleanly():
    """The live socket interleaves two price series for INFY (about 992 and 1016 rupees); every packet still decodes."""
    seen = set()
    for f in _frames("ws_market_full_dual_series.json"):
        for p in wire.parse_market_frame(f):
            assert p["tc"] is not None, p
            if p.get("token") == 1594:
                seen.add(p["ltp"] // 1000)
    assert any(x <= 99 for x in seen)


def test_tc3_index_nsefo_and_bse_layouts():
    idx = struct.pack(">HHiiiiiii", 3, 3, 26000, 2248435, 2223180, 2230000, 2255000, 2220000, 0) + struct.pack(">i", 0)
    assert len(idx) == 36
    fo = bytearray(234)
    struct.pack_into(">HHHi", fo, 0, 3, 2, 7, 35008)
    struct.pack_into(">iiiq", fo, 10, 5000, 65, 4990, 130000)
    struct.pack_into(">iiii", fo, 46, 4800, 4900, 5100, 4700)
    struct.pack_into(">qih", fo, 66, 650, 4995, 3)
    struct.pack_into(">qih", fo, 66 + 70, 130, 5005, 2)
    struct.pack_into(">q", fo, 206, 987654)
    bse = bytearray(262)
    struct.pack_into(">HHii", bse, 0, 3, 4, 500325, 120500)
    struct.pack_into(">iiii", bse, 48, 120000, 120100, 121000, 119900)
    struct.pack_into(">qii", bse, 68, 10, 120450, 4)
    struct.pack_into(">qii", bse, 68 + 80, 12, 120550, 5)
    pk = wire.parse_market_frame(idx + bytes(fo) + bytes(bse) + b"\x00\x0a")
    assert [p["tc"] for p in pk] == [3, 3, 3, 10]
    assert pk[0]["exchange"] == "NSEIDX" and pk[0]["ltp"] == 2248435 and pk[0]["close"] == 2223180
    assert pk[1]["exchange"] == "NSEFO" and pk[1]["token"] == 35008 and pk[1]["ltp"] == 5000 and pk[1]["close"] == 4800 and pk[1]["bid"] == 4995 and pk[1]["ask"] == 5005 and pk[1]["oi"] == 987654
    assert pk[2]["exchange"] == "BSEEQ" and pk[2]["ltp"] == 120500 and pk[2]["bid"] == 120450 and pk[2]["ask"] == 120550 and pk[2]["close"] == 120000


def test_tc9_option_chain_fields_in_the_order_sent():
    body = struct.pack(">HiHB", 9, 35008, 2, 4)
    body += b"\x01" + struct.pack(">i", 5000)
    body += b"\x02" + struct.pack(">q", 1234567)
    body += b"\x04" + struct.pack(">i", 2534)
    body += b"\x08" + struct.pack(">iiiii", 5100, 20, 300, -150, 10)
    pk = wire.parse_market_frame(body + struct.pack(">HHii", 1, 2, 35009, 4100))
    assert pk[0] == {"tc": 9, "exchange": "NSEFO", "token": 35008, "ltp": 5000, "oi": 1234567, "iv": 2534, "delta": 5100, "gamma": 20, "vega": 300, "theta": -150, "rho": 10}
    assert pk[1]["tc"] == 1 and pk[1]["token"] == 35009


def test_heartbeat_and_truncated_or_unknown_packets_are_never_guessed():
    assert wire.parse_market_frame(b"\x00\x0a") == [{"tc": 10}]
    good = struct.pack(">HHii", 1, 1, 2885, 120700)
    out = wire.parse_market_frame(good + good[:7])
    assert out[0]["ltp"] == 120700 and out[1]["tc"] is None and "truncated" in out[1]["error"]
    out = wire.parse_market_frame(struct.pack(">HH", 77, 1) + good)
    assert out == [{"tc": None, "error": "unknown transaction code 77", "offset": 0}]


def test_tc4_order_events_from_the_live_capture():
    evs = [wire.parse_order_event(f) for f in _frames("ws_orders_events.json")]
    assert [e["event"] for e in evs] == ["ACCEPTED", "MODIFY_ACCEPTED", "CANCELLED"]
    assert all(e["order_id"] == 256159 and e["symbol"] == "IDEA" and e["ucc"] == "HACK0000" for e in evs)
    assert evs[0]["qty"] == 1 and evs[0]["price"] == 1187 and evs[1]["price"] == 1188 and evs[2]["qty"] == 0


def test_tc8_bse_event_with_reason_and_heartbeat():
    raw = struct.pack(">HH", 8, 3) + b"HACK0000  " + struct.pack(">Hiiii", 4, 500325, 777, -5, 120500) + b"RMS: insufficient funds"
    e = wire.parse_order_event(raw)
    assert e["event"] == "REJECTED" and e["exchange"] == "BSEEQ" and e["token"] == 500325 and e["order_id"] == 777 and e["qty"] == -5 and e["reason"] == "RMS: insufficient funds"
    assert wire.parse_order_event(b"\x00\x0a")["event"] == "HEARTBEAT"


def test_tick_payload_uses_the_packet_close_and_falls_back_to_ltp_for_missing_depth():
    t = tick_payload({"instrument_id": "NSE:EQ:RELIANCE", "symbol": "RELIANCE", "prev_close": 120770}, {"ltp": 120780, "bid": 120770, "ask": 0, "close": 121800, "volume": 5}, 7)
    assert t["ltp_paise"] == 120780 and t["bid_paise"] == 120770 and t["ask_paise"] == 120780 and t["prev_close_paise"] == 121800 and t["seq"] == 7 and t["src"] == "021"
