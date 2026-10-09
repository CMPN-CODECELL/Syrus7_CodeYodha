"""MarketDataProvider: reads the latest ticks published by the feed (021 WebSocket adapter or replay)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone

from countersign.core import events
from countersign.core.config import settings

IST = timezone(timedelta(hours=5, minutes=30))
NSE_HOLIDAYS_2026 = {date(2026, 10, 2), date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25)}


@dataclass
class Tick:
    instrument_id: str
    symbol: str
    ltp_paise: int
    bid_paise: int
    ask_paise: int
    volume: int
    prev_close_paise: int
    ts: datetime
    seq: int

    @property
    def age_ms(self) -> int:
        return int((datetime.now(UTC) - self.ts).total_seconds() * 1000)

    @property
    def stale(self) -> bool:
        return self.age_ms > settings().tick_stale_seconds * 1000

    def to_api(self) -> dict:
        change = self.ltp_paise - self.prev_close_paise
        return {
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "ltp_paise": self.ltp_paise,
            "bid_paise": self.bid_paise,
            "ask_paise": self.ask_paise,
            "volume": self.volume,
            "prev_close_paise": self.prev_close_paise,
            "change_paise": change,
            "change_bps": int(change * 10000 / self.prev_close_paise) if self.prev_close_paise else 0,
            "ts": self.ts.isoformat().replace("+00:00", "Z"),
            "seq": self.seq,
            "stale": self.stale,
        }

    @staticmethod
    def from_json(raw: str) -> Tick:
        d = json.loads(raw)
        return Tick(
            instrument_id=d["instrument_id"],
            symbol=d["symbol"],
            ltp_paise=int(d["ltp_paise"]),
            bid_paise=int(d["bid_paise"]),
            ask_paise=int(d["ask_paise"]),
            volume=int(d.get("volume", 0)),
            prev_close_paise=int(d.get("prev_close_paise", d["ltp_paise"])),
            ts=datetime.fromisoformat(d["ts"].replace("Z", "+00:00")),
            seq=int(d.get("seq", 0)),
        )


def latest(instrument_id: str) -> Tick | None:
    try:
        raw = events.client().get(events.TICK_KEY.format(instrument_id))
    except Exception:
        return None
    return Tick.from_json(raw) if raw else None


def latest_many(instrument_ids: list[str]) -> dict[str, Tick]:
    if not instrument_ids:
        return {}
    try:
        raws = events.client().mget([events.TICK_KEY.format(i) for i in instrument_ids])
    except Exception:
        return {}
    return {i: Tick.from_json(r) for i, r in zip(instrument_ids, raws, strict=True) if r}


def history(instrument_id: str, points: int = 300) -> list[dict]:
    try:
        rows = events.client().lrange(events.HISTORY_KEY.format(instrument_id), -points, -1)
    except Exception:
        return []
    out = []
    for r in rows:
        d = json.loads(r)
        out.append({"time": int(d["t"]), "value": d["p"] / 100})
    return out


def session_state(now: datetime | None = None) -> dict:
    if settings().market_always_open:
        return {"open": True, "label": "Replay session (simulated market, demo mode)"}
    now_ist = (now or datetime.now(UTC)).astimezone(IST)
    if now_ist.weekday() >= 5 or now_ist.date() in NSE_HOLIDAYS_2026:
        return {"open": False, "label": "Market closed (weekend or exchange holiday)"}
    minutes = now_ist.hour * 60 + now_ist.minute
    if 9 * 60 + 15 <= minutes < 15 * 60 + 30:
        return {"open": True, "label": "Normal market session"}
    if 9 * 60 <= minutes < 9 * 60 + 15:
        return {"open": False, "label": "Pre-open session (continuous trading starts 09:15)"}
    return {"open": False, "label": "Market closed (outside 09:15 to 15:30 IST)"}
