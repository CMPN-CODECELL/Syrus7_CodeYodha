"""021 instrument file -> compact table -> Countersign working set (a slice of the real file). Real symbols resolve to
021 tokens, NSE stocks not in the seed are added on first use, fictional Attack Lab instruments stay, look-alike
detection still works. The seed is restored afterwards so other tests see the fictional market."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from countersign.broker021 import instruments021
from countersign.core import instruments
from countersign.core.db import session_scope
from countersign.core.models import Instrument

FIX = Path(__file__).parent / "fixtures" / "021"


@pytest.fixture
def loaded():
    rows = instruments021.parse_csv((FIX / "instruments_sample.csv").read_text(encoding="utf-8"))
    n = instruments021.load_table(rows)
    stats = instruments021.upsert_working_set()
    yield n, stats
    with session_scope() as s:
        s.execute(text("DELETE FROM instruments WHERE token_021 IS NOT NULL OR source = '021' OR flags ? 'REPLAY_ONLY'"))
        s.execute(text("DELETE FROM instruments_021"))
        instruments.seed(s)


def test_compact_table_and_working_set(loaded):
    n, stats = loaded
    assert n >= 15 and stats["indices"] == 2 and stats["options"] == 10
    with session_scope() as s:
        rel = s.get(Instrument, "NSE:EQ:RELIANCE")
        assert (rel.exchange_021, rel.token_021, rel.tick_size_paise) == ("NSECM", 2885, 10)
        assert (rel.lower_circuit_paise, rel.upper_circuit_paise) == (108700, 132840)
        assert rel.prev_close_paise == (108700 + 132840) // 2 - ((108700 + 132840) // 2) % 10
        assert s.get(Instrument, "NSE:IDX:NIFTY").token_021 == 26000 and s.get(Instrument, "NSE:IDX:BANKNIFTY").token_021 == 26009
        opt = s.get(Instrument, instruments.option_id("2026-10-13", 22500, "CE"))
        assert opt is not None and opt.lot_size == 65 and opt.exchange_021 == "NSEFO" and opt.expiry == "2026-10-13"
        assert s.get(Instrument, "NSE:EQ:ABCCORP").token_021 is None  # fictional: replay only, never sent to 021


def test_real_symbols_resolve_and_new_stocks_are_added_on_first_use(loaded):
    with session_scope() as s:
        r = instruments.resolve(s, "TMPV")
        assert r.status == "RESOLVED" and r.instrument.token_021 == 3456 and r.instrument.display_name == "Tata Motors Passenger Vehicles"
        assert instruments.resolve(s, "tata motors pv").instrument.symbol == "TMPV"
        old = instruments.resolve(s, "TATAMOTORS").instrument  # demerged: replay only, labelled as such
        assert old.token_021 is None and "REPLAY_ONLY" in old.flags and "not on 021" in old.display_name
        assert instruments.resolve(s, "RΕLIANCE").status == "LOOKALIKE"
        assert instruments.resolve(s, "reliance").instrument.instrument_id == "NSE:EQ:RELIANCE"
        assert instruments.resolve(s, "NOSUCHSTOCK").status == "NOT_FOUND"


def test_expiry_timestamps_are_ist_dates():
    assert instruments.expiry_date_ist(1791882000) == "2026-10-13"
    assert instruments.expiry_date_ist(1792400400) == "2026-10-19"


def test_lookup_maps_tokens_both_ways_and_materializes_unknown_stocks(loaded):
    lk = instruments021.Lookup()
    assert lk.token_for("NSE:EQ:INFY") == ("NSECM", 1594)
    assert lk.instrument_for("NSECM", 1594)["instrument_id"] == "NSE:EQ:INFY"
    idea = lk.instrument_for("NSECM", 14366)
    assert idea["instrument_id"] == "NSE:EQ:IDEA"
    assert lk.instrument_for("NSECM", 999999)["instrument_id"] == "021:NSECM:999999"
    assert lk.instrument_for_isin("INE467B01029")["instrument_id"] == "NSE:EQ:TCS"
