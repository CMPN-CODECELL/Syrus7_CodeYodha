"""021 instrument file: daily download (cached as gzip), compact copy in Postgres, and the working set of Countersign
instruments (curated stocks, NIFTY / BANKNIFTY, NIFTY options for the nearest expiries) mapped to 021 tokens.

The download needs the access token, so it runs in the gateway; everything it loads is credential-free data that the
API process reads to resolve symbols."""

from __future__ import annotations

import csv
import gzip
import io
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text

from countersign.core import instruments as master
from countersign.core.db import engine, session_scope
from countersign.core.models import Instrument, Instrument021

COLUMNS = ["exchange", "token", "symbol", "instrument_type", "lot", "tick", "freeze", "lower_circuit", "upper_circuit", "expiry", "strike", "option_type", "isin", "underlying_token", "underlying_exchange"]
IST = timedelta(hours=5, minutes=30)
OPTION_EXPIRIES_LOADED = 2


def ist_today() -> str:
    return (datetime.now(UTC) + IST).date().isoformat()


def _int(v: str | None) -> int:
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def parse_csv(text_: str) -> list[tuple]:
    out = []
    for r in csv.DictReader(io.StringIO(text_)):
        out.append(
            (
                r["exchange"].strip(),
                _int(r["token"]),
                (r.get("symbol") or "").strip()[:64],
                (r.get("instrument_type") or "").strip()[:8],
                _int(r.get("board_lot_quantity")),
                _int(r.get("ticksize")),
                _int(r.get("freeze_quantity")),
                _int(r.get("lower_circuit")),
                _int(r.get("upper_circuit")),
                _int(r.get("expiry")),
                _int(r.get("strike_price")),
                (r.get("option_type") or "").strip()[:4],
                (r.get("isin") or "").strip()[:16],
                _int(r.get("underlying_token")),
                (r.get("underlying_exchange") or "").strip()[:8],
            )
        )
    return out


def decode(raw: bytes) -> str:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def cached(path: Path) -> bytes | None:
    """Today's cached file (IST date of its modification), or None."""
    if not path.exists():
        return None
    mtime_ist = (datetime.fromtimestamp(path.stat().st_mtime, UTC) + IST).date().isoformat()
    return path.read_bytes() if mtime_ist == ist_today() else None


def download(client, path: Path) -> bytes:
    r = client.get("/instruments", raw=True)
    raw = r.content
    gz = raw if raw[:2] == b"\x1f\x8b" else gzip.compress(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gz)
    tmp.replace(path)
    return gz


def load_table(rows: list[tuple]) -> int:
    """Replaces instruments_021 in one transaction (COPY), so readers see either the old or the new file."""
    raw = engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute("DELETE FROM instruments_021")
            seen = set()
            with cur.copy(f"COPY instruments_021 ({', '.join(chr(34) + c + chr(34) for c in COLUMNS)}) FROM STDIN") as cp:
                for row in rows:
                    key = (row[0], row[1])
                    if key in seen:
                        continue
                    seen.add(key)
                    cp.write_row(row)
        raw.commit()
    finally:
        raw.close()
    return len(seen)


def table_count() -> int:
    with session_scope() as s:
        return int(s.execute(text("SELECT count(*) FROM instruments_021")).scalar() or 0)


def _stock_row(s, symbol: str) -> Instrument021 | None:
    return s.execute(select(Instrument021).where(Instrument021.exchange == "NSECM", Instrument021.symbol == symbol, Instrument021.instrument_type == "STK").order_by(Instrument021.token)).scalars().first()


def upsert_working_set() -> dict:
    """Maps the curated stocks, the two indices and NIFTY options of the nearest expiries to 021 tokens."""
    stats = {"stocks": 0, "indices": 0, "options": 0, "missing": []}
    with session_scope() as s:
        symbols = [x[0] for x in master.EQUITIES] + [x[0] for x in master.EXTRA_021]
        for sym in symbols:
            if sym in master.REPLAY_ONLY:
                continue
            row = _stock_row(s, sym)
            if row is None:
                stats["missing"].append(sym)
                continue
            inst = s.get(Instrument, f"NSE:EQ:{sym}")
            if inst is None:
                s.add(master.equity_from_021(row))
            else:
                was_seed = inst.token_021 is None
                master.apply_021_row(inst, row)
                if was_seed:
                    # Seed prices are fictional; start from the middle of today's real circuit band until the feed
                    # reports the previous close.
                    inst.prev_close_paise = master._mid_circuit(row)
                cur = master._curated(sym)
                if cur and inst.source == "021":
                    inst.aliases = cur[1]
            stats["stocks"] += 1
        for sym in master.REPLAY_ONLY:
            # Fictional instruments stay for the replay feed and the Attack Lab; they are labelled so a trader on the
            # 021 account sees they are not tradable there (the gateway refuses them: INSTRUMENT_NOT_ON_021).
            inst = s.get(Instrument, f"NSE:EQ:{sym}")
            if inst is not None and "REPLAY_ONLY" not in (inst.flags or []):
                inst.flags = [*(inst.flags or []), "REPLAY_ONLY"]
                if "replay only" not in inst.display_name:
                    inst.display_name = f"{inst.display_name} (replay only, not on 021)"
        for sym, (token, name, aliases) in master.INDEX_021.items():
            row = s.execute(select(Instrument021).where(Instrument021.exchange == "NSEIDX", Instrument021.token == token)).scalars().first()
            if row is None:
                stats["missing"].append(sym)
                continue
            inst = s.get(Instrument, f"NSE:IDX:{sym}")
            if inst is None:
                inst = Instrument(instrument_id=f"NSE:IDX:{sym}", exchange="NSE", segment="IDX", symbol=sym, display_name=name, raw_name=name, tick_size_paise=5, status="ACTIVE", prev_close_paise=0, lower_circuit_paise=0, upper_circuit_paise=0, aliases=aliases, flags=[], source="021")
                s.add(inst)
            inst.token_021 = token
            inst.exchange_021 = "NSEIDX"
            stats["indices"] += 1
        now = int(time.time())
        expiries = sorted(set(s.execute(select(Instrument021.expiry).where(Instrument021.exchange == "NSEFO", Instrument021.symbol == "NIFTY", Instrument021.instrument_type == "OPTIDX", Instrument021.expiry >= now)).scalars()))[:OPTION_EXPIRIES_LOADED]
        if expiries:
            rows = s.execute(select(Instrument021).where(Instrument021.exchange == "NSEFO", Instrument021.symbol == "NIFTY", Instrument021.instrument_type == "OPTIDX", Instrument021.expiry.in_(expiries))).scalars().all()
            fresh = [master.option_from_021(r) for r in rows]
            existing = {i.instrument_id: i for i in s.execute(select(Instrument).where(Instrument.instrument_id.in_([f.instrument_id for f in fresh]))).scalars()}
            for f, r in zip(fresh, rows, strict=True):
                cur = existing.get(f.instrument_id)
                if cur is None:
                    s.add(f)
                else:
                    master.apply_021_row(cur, r)
                    cur.lot_size = f.lot_size
                    if cur.source != "021":
                        cur.prev_close_paise = f.prev_close_paise
                        cur.source = "021"
                stats["options"] += 1
    return stats


def ensure_loaded(client, cache_path: str, force: bool = False) -> dict:
    """Downloads today's file once (cached), reloads the table when the cache is newer, then maps the working set."""
    path = Path(cache_path)
    t0 = time.time()
    raw = None if force else cached(path)
    source = "cache"
    if raw is None:
        raw = download(client, path)
        source = "download"
    rows = parse_csv(decode(raw))
    n = load_table(rows)
    ws = upsert_working_set()
    return {"rows": n, "source": source, "date": ist_today(), "seconds": round(time.time() - t0, 2), **ws}


# ------------------------------------------------------------------ lookups (cached in-process)


class Lookup:
    """instrument_id <-> (021 exchange, token). Misses fall back to the database (and materialize stock and NIFTY
    option rows on first use, so positions, holdings and orders always carry resolvable instrument ids)."""

    def __init__(self) -> None:
        self.by_id: dict[str, tuple[str, int]] = {}
        self.by_tok: dict[tuple[str, int], dict] = {}
        self.loaded_at = 0.0

    def refresh(self) -> None:
        with session_scope() as s:
            rows = s.execute(select(Instrument.instrument_id, Instrument.symbol, Instrument.segment, Instrument.exchange_021, Instrument.token_021).where(Instrument.token_021.is_not(None))).all()
        by_id, by_tok = {}, {}
        for iid, sym, seg, ex, tok in rows:
            by_id[iid] = (ex, int(tok))
            by_tok[(ex, int(tok))] = {"instrument_id": iid, "symbol": sym, "segment": seg}
        self.by_id, self.by_tok, self.loaded_at = by_id, by_tok, time.time()

    def _maybe_refresh(self) -> None:
        if time.time() - self.loaded_at > 30:
            self.refresh()

    def token_for(self, instrument_id: str) -> tuple[str, int] | None:
        self._maybe_refresh()
        hit = self.by_id.get(instrument_id)
        if hit is None:
            self.refresh()
            hit = self.by_id.get(instrument_id)
        return hit

    def instrument_for(self, exchange: str, token: int) -> dict:
        self._maybe_refresh()
        key = (exchange, int(token))
        hit = self.by_tok.get(key)
        if hit is not None:
            return hit
        with session_scope() as s:
            row = s.execute(select(Instrument021).where(Instrument021.exchange == exchange, Instrument021.token == int(token))).scalars().first()
            inst = None
            if row is not None and exchange == "NSECM" and row.instrument_type == "STK" and master.is_ascii_symbol(row.symbol):
                inst = s.get(Instrument, f"NSE:EQ:{row.symbol}") or master.materialize_021(s, row.symbol)
            elif row is not None and exchange == "NSEFO" and row.symbol == "NIFTY" and row.instrument_type == "OPTIDX":
                fresh = master.option_from_021(row)
                inst = s.get(Instrument, fresh.instrument_id)
                if inst is None:
                    s.add(fresh)
                    s.flush()
                    inst = fresh
                elif inst.token_021 is None:
                    master.apply_021_row(inst, row)
            if inst is not None and inst.token_021 == int(token):
                out = {"instrument_id": inst.instrument_id, "symbol": inst.symbol, "segment": inst.segment}
            else:
                from countersign.broker021.mapping import SEGMENT_OF

                out = {"instrument_id": f"021:{exchange}:{token}", "symbol": (row.symbol if row is not None and row.symbol else str(token)), "segment": SEGMENT_OF.get(exchange, "EQ")}
        self.by_tok[key] = out
        return out

    def instrument_for_isin(self, isin: str, symbol: str | None = None) -> dict | None:
        with session_scope() as s:
            row = s.execute(select(Instrument021).where(Instrument021.isin == isin, Instrument021.exchange == "NSECM", Instrument021.instrument_type == "STK")).scalars().first()
            if row is None and symbol:
                row = _stock_row(s, symbol)
            if row is None:
                return None
            token = row.token
        return self.instrument_for("NSECM", token)
