"""Market feed: replays a deterministic, seeded market into Valkey. In production this process is the 021
WebSocket adapter; for the hackathon it is a tick replay with scripted scenarios so demos never depend on luck."""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from countersign.core import events
from countersign.core.db import session_scope
from countersign.core.models import Instrument
from countersign.core.pricing import option_premium_paise

TICK_INTERVAL = 1.0


@dataclass
class Sym:
    inst_id: str
    symbol: str
    prev_close: int
    tick: int
    ltp: int
    segment: str
    vol_bps: int = 12
    volume: int = 0
    mode: str = "walk"  # walk | hold | path | stale
    path: list[int] = field(default_factory=list)
    stale_until: float = 0.0
    seq: int = 0
    strike: int | None = None
    opt: str | None = None
    expiry: str | None = None


class Feed:
    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.syms: dict[str, Sym] = {}
        self.lock = threading.Lock()
        self.mode = "replay"

    def load(self) -> None:
        with session_scope() as s:
            rows = s.execute(select(Instrument)).scalars().all()
            from countersign.core.instruments import nifty_expiries

            near = set(nifty_expiries()[:2])
            for r in rows:
                if r.segment == "FO" and r.expiry not in near:
                    continue
                if r.segment == "EQ" and r.status != "ACTIVE" and r.symbol != "XYZSUSP":
                    continue
                start = r.prev_close_paise
                if r.segment == "EQ" and r.symbol not in ("ABCCORP",):
                    drift = self.rng.randint(-150, 150)
                    start = r.prev_close_paise * (10000 + drift) // 10000
                    start -= start % r.tick_size_paise
                if r.symbol == "TATAMOTORS":
                    start = 91285
                self.syms[r.instrument_id] = Sym(
                    inst_id=r.instrument_id, symbol=r.symbol, prev_close=r.prev_close_paise, tick=r.tick_size_paise,
                    ltp=start, segment=r.segment, vol_bps=6 if r.segment == "IDX" else 10,
                    strike=r.strike_paise, opt=r.option_type, expiry=r.expiry,
                    mode="hold" if r.symbol == "ABCCORP" else "walk",
                )

    def _round(self, s: Sym, p: int) -> int:
        p = max(s.tick, p)
        return p - p % s.tick

    def step(self) -> list[dict]:
        out = []
        now = time.time()
        with self.lock:
            spot = self.syms.get("NSE:IDX:NIFTY")
            for s in self.syms.values():
                if s.segment == "FO":
                    continue
                if s.mode == "stale" and now < s.stale_until:
                    continue
                if s.mode == "stale":
                    s.mode = "walk"
                if s.mode == "path" and s.path:
                    s.ltp = s.path.pop(0)
                    if not s.path:
                        s.mode = "hold"
                elif s.mode == "walk":
                    shock = self.rng.gauss(0, s.vol_bps)
                    s.ltp = self._round(s, int(s.ltp * (1 + shock / 10000)))
                s.volume += self.rng.randint(50, 900)
                out.append(self._emit(s))
            if spot is not None:
                for s in self.syms.values():
                    if s.segment != "FO":
                        continue
                    if s.mode == "stale" and now < s.stale_until:
                        continue
                    s.ltp = option_premium_paise(spot.ltp, s.strike, s.opt, s.expiry)
                    s.volume += self.rng.randint(65, 6500)
                    out.append(self._emit(s))
        return out

    def _emit(self, s: Sym) -> dict:
        s.seq += 1
        spread = max(s.tick, s.ltp // 4000)
        spread -= spread % s.tick or 0
        spread = max(spread, s.tick)
        return {
            "instrument_id": s.inst_id,
            "symbol": s.symbol,
            "ltp_paise": s.ltp,
            "bid_paise": s.ltp - spread,
            "ask_paise": s.ltp + spread,
            "volume": s.volume,
            "prev_close_paise": s.prev_close,
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "seq": s.seq,
        }

    def control(self, cmd: dict) -> None:
        name = cmd.get("cmd")
        with self.lock:
            if name == "set":
                s = self.syms.get(cmd["instrument_id"])
                if s:
                    s.ltp = self._round(s, int(cmd["ltp_paise"]))
                    s.mode = "hold"
                    publish([self._emit(s)])
                return
            if name != "scenario":
                return
            scen = cmd.get("name", "calm")
            target = None
            if cmd.get("instrument_id"):
                target = self.syms.get(cmd["instrument_id"])
            if scen == "calm":
                for s in self.syms.values():
                    if s.segment != "FO":
                        s.mode = "walk" if s.symbol != "ABCCORP" else "hold"
                return
            if target is None:
                return
            p = target.ltp
            if scen == "dip_to_trigger":
                if target.symbol == "ABCCORP":
                    seq = [50500, 50250, 50000, 49900, 49900, 49950, 49950]
                else:
                    seq = [p * (10000 - k) // 10000 for k in (0, 150, 300, 450, 600, 650, 650)]
                target.path = [self._round(target, x) for x in seq]
                target.mode = "path"
            elif scen == "spike":
                target.path = [self._round(target, p * 10300 // 10000)] * 3
                target.mode = "path"
            elif scen == "crash":
                target.path = [self._round(target, p * (10000 - k) // 10000) for k in (150, 300, 450, 600, 700, 700)]
                target.mode = "path"
            elif scen == "stale":
                target.mode = "stale"
                target.stale_until = time.time() + float(cmd.get("seconds", 12))
            elif scen == "hold":
                target.mode = "hold"


def publish(ticks: list[dict]) -> None:
    if not ticks:
        return
    c = events.client()
    pipe = c.pipeline(transaction=False)
    ts_sec = int(time.time())
    for t in ticks:
        raw = json.dumps(t)
        pipe.set(events.TICK_KEY.format(t["instrument_id"]), raw)
        hk = events.HISTORY_KEY.format(t["instrument_id"])
        pipe.rpush(hk, json.dumps({"t": ts_sec, "p": t["ltp_paise"]}))
        pipe.ltrim(hk, -900, -1)
        pipe.publish(events.CH_TICKS, raw)
    pipe.execute()


def _backfill(feed: Feed) -> None:
    """Seed ~10 minutes of history so charts render immediately."""
    c = events.client()
    now = int(time.time())
    rng = random.Random(11)
    for s in feed.syms.values():
        if s.segment == "FO":
            continue
        hk = events.HISTORY_KEY.format(s.inst_id)
        if c.llen(hk) > 100:
            continue
        p = s.ltp
        pts = []
        for i in range(600, 0, -1):
            p = max(s.tick, int(p * (1 + rng.gauss(0, s.vol_bps) / 10000)))
            pts.append(json.dumps({"t": now - i, "p": p - p % s.tick}))
        c.rpush(hk, *pts)
        c.ltrim(hk, -900, -1)


def _control_listener(feed: Feed) -> None:
    while True:
        try:
            ps = events.client().pubsub(ignore_subscribe_messages=True)
            ps.subscribe(events.CH_FEED_CONTROL)
            for msg in ps.listen():
                if msg.get("type") == "message":
                    cmd = json.loads(msg["data"])
                    if cmd.get("cmd") == "reset":
                        with feed.lock:
                            feed.syms.clear()
                        feed.load()
                        continue
                    feed.control(cmd)
        except Exception:
            time.sleep(1.0)


def main() -> None:
    feed = Feed()
    feed.load()
    _backfill(feed)
    threading.Thread(target=_control_listener, args=(feed,), daemon=True).start()
    events.client().set("cs:feed:status", json.dumps({"mode": feed.mode, "started": time.time()}))
    while True:
        t0 = time.time()
        try:
            publish(feed.step())
            events.client().set("cs:feed:heartbeat", str(time.time()))
        except Exception as e:  # keep the feed alive across transient Valkey errors
            print("feed error:", e)
        time.sleep(max(0.05, TICK_INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
