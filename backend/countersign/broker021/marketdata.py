"""021 market-data websocket -> Valkey ticks in Countersign's Tick format (the same keys the replay feed writes), so the
verifier, mandate engine, monitor and UI work unchanged.

- Subscribes, in full mode (LTP, best bid/ask, volume, previous close), every 021-backed stock and index in the
  instrument master, instruments of active standing instructions, open 021 orders, and instruments the API asked for
  (option-chain strikes, quotes): at most 95 per connection, more connections when needed.
- Reconnects with exponential backoff and jitter, fetching a fresh ephemeral key each time and resubscribing (the
  server forgets subscriptions; it also drops every socket at 08:00 IST).
- The socket sends only instruments that changed. While a connection is provably alive (a frame or a pong within the
  last few seconds), unchanged instruments are re-stamped once a second: their last value is still the latest.
  When the connection is not alive, nothing is re-stamped, so ticks go stale and the verifier blocks trading.
- An instrument the Attack Lab is driving (cs:feed:override:<id>) is left to the replay feed while the override lasts."""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import select, text

from countersign.broker021 import wire
from countersign.broker021.wire import redact
from countersign.core import events
from countersign.core.db import session_scope
from countersign.core.market import WANT_021_KEY
from countersign.core.models import Instrument, StandingMandate

PER_CONN = 95
MAX_CONNS = 10
ALIVE_S = 4.0
OVERRIDE_PREFIX = "cs:feed:override:"


def tick_payload(info: dict, last: dict, seq: int, now: datetime | None = None) -> dict:
    ltp = int(last["ltp"])
    bid = int(last.get("bid") or 0) or ltp
    ask = int(last.get("ask") or 0) or ltp
    prev = int(last.get("close") or 0) or int(info.get("prev_close") or 0) or ltp
    return {
        "instrument_id": info["instrument_id"],
        "symbol": info["symbol"],
        "ltp_paise": ltp,
        "bid_paise": bid,
        "ask_paise": ask,
        "volume": int(last.get("volume") or 0),
        "prev_close_paise": prev,
        "ts": (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "seq": seq,
        "src": "021",
    }


class Conn:
    def __init__(self, md: MarketData, idx: int):
        self.md = md
        self.idx = idx
        self.tokens: set[tuple[int, int]] = set()  # wanted on this connection
        self.subscribed: set[tuple[int, int]] = set()
        self.ws = None
        self.alive_at = 0.0
        self.frames = 0
        self.connects = 0
        self.last_error: str | None = None

    @property
    def alive(self) -> bool:
        return self.ws is not None and time.time() - self.alive_at < ALIVE_S

    async def _send(self, ws, task: str, items: list[tuple[int, int]]) -> None:
        for i in range(0, len(items), 90):
            msg = json.dumps({"Task": task, "Mode": "full", "Instruments": [[c, t] for c, t in items[i : i + 90]]})
            await ws.send(msg)

    async def sync_subs(self, ws) -> None:
        add = sorted(self.tokens - self.subscribed)
        drop = sorted(self.subscribed - self.tokens)
        if drop:
            await self._send(ws, "unsubscribe", drop)
            self.subscribed -= set(drop)
        if add:
            await self._send(ws, "subscribe", add)
            self.subscribed |= set(add)

    async def _reader(self, ws) -> None:
        async for msg in ws:
            self.alive_at = time.time()
            if isinstance(msg, (bytes, bytearray)):
                self.frames += 1
                self.md.on_frame(bytes(msg))

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                pong = await ws.ping()
                await asyncio.wait_for(pong, 5.0)
                self.alive_at = time.time()
            except Exception:  # noqa: BLE001  (no pong: not provably alive; the reader decides about the socket)
                pass

    async def run(self) -> None:
        import websockets

        backoff = 1.0
        while True:
            if not self.tokens:
                await asyncio.sleep(1.0)
                continue
            try:
                data = await asyncio.to_thread(self.md.gw.client.get, "/websocket/ephemeral-key")
                key = ((data or {}).get("data") or {}).get("token")
                if not key:
                    raise RuntimeError("021 returned no ephemeral key")
                async with websockets.connect(f"{self.md.ws_base}/market?token={key}", max_size=2**22, open_timeout=15, ping_interval=20, ping_timeout=20) as ws:
                    self.ws = ws
                    self.connects += 1
                    self.alive_at = time.time()
                    self.subscribed = set()
                    backoff = 1.0
                    reader = asyncio.create_task(self._reader(ws))
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        while not reader.done():
                            await self.sync_subs(ws)
                            await asyncio.sleep(1.0)
                        reader.result()
                    finally:
                        pinger.cancel()
                        reader.cancel()
                self.last_error = "socket closed by the server"
            except Exception as e:  # noqa: BLE001
                self.last_error = redact(f"{type(e).__name__}: {str(e)[:160]}")
            finally:
                self.ws = None
                self.subscribed = set()
            await asyncio.sleep(backoff + random.uniform(0, backoff))
            backoff = min(backoff * 2, 30.0)


class MarketData:
    def __init__(self, gw, ws_base: str, publish_ticks: bool = True, isolated: bool = False, median: int = 3):
        self.gw = gw
        self.ws_base = ws_base
        self.publish_ticks = publish_ticks
        self.isolated = isolated
        self.median = max(1, median)
        self.recent: dict[tuple[int, int], list[int]] = {}
        self.raw_updates = 0
        self.suppressed = 0
        self.dirty = False  # a new instrument was requested: recompute subscriptions now
        self.trace: dict[str, list[tuple]] = {}  # instrument_id -> last raw updates (diagnostics, admin only)
        self.raw_frames: list[str] = []  # last raw frames as hex (diagnostics, admin only; market data carries no account data)
        self.want: dict[tuple[int, int], dict] = {}
        self.requested: dict[str, float] = {}  # instrument_id -> until (requests from the API)
        self.conns: list[Conn] = []
        self.last: dict[tuple[int, int], dict] = {}
        self.seq: dict[tuple[int, int], int] = {}
        self.published_at: dict[tuple[int, int], float] = {}
        self.hist_sec: dict[tuple[int, int], int] = {}
        self.closes_saved: dict[str, int] = {}
        self.overrides: set[str] = set()
        self.ticks_published = 0
        self.decode_errors = 0
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ what to subscribe

    def request(self, instrument_ids: list[str], ttl_s: float = 600.0) -> int:
        until = time.time() + ttl_s
        n = 0
        with self.lock:
            for iid in instrument_ids:
                if self.gw.lookup.token_for(iid) is not None:
                    if iid not in self.requested:
                        self.dirty = True
                    self.requested[iid] = until
                    n += 1
        return n

    def compute_want(self) -> dict[tuple[int, int], dict]:
        want: dict[tuple[int, int], dict] = {}
        now = time.time()
        with self.lock:
            self.requested = {k: v for k, v in self.requested.items() if v > now}
            extra = set(self.requested)
        with session_scope() as s:
            extra |= set(s.execute(select(StandingMandate.instrument_id).where(StandingMandate.state.in_(["PENDING_APPROVAL", "ACTIVE", "TRIGGERED", "EXECUTING"]))).scalars())
            q = select(Instrument.instrument_id, Instrument.symbol, Instrument.segment, Instrument.exchange_021, Instrument.token_021, Instrument.prev_close_paise).where(Instrument.token_021.is_not(None))
            for iid, sym, seg, ex, tok, prev in s.execute(q).all():
                if seg in ("EQ", "IDX") or iid in extra:
                    code = wire.EXCHANGE_CODES.get(ex)
                    if code:
                        want[(code, int(tok))] = {"instrument_id": iid, "symbol": sym, "segment": seg, "prev_close": prev}
        for row in list(self.gw._book.values()):
            if row.get("status") in ("Executed", "Rejected", "Cancelled"):
                continue
            code = wire.EXCHANGE_CODES.get(row.get("exchange") or "")
            if code and (code, int(row.get("token") or 0)) not in want:
                info = self.gw.lookup.instrument_for(row["exchange"], int(row["token"]))
                want[(code, int(row["token"]))] = info | {"prev_close": 0}
        return want

    def assign(self, want: dict[tuple[int, int], dict]) -> None:
        self.want = want
        keys = set(want)
        for c in self.conns:
            c.tokens &= keys
        placed = set().union(*(c.tokens for c in self.conns)) if self.conns else set()
        for k in sorted(keys - placed):
            target = next((c for c in self.conns if len(c.tokens) < PER_CONN), None)
            if target is None:
                if len(self.conns) >= MAX_CONNS:
                    break
                target = Conn(self, len(self.conns))
                self.conns.append(target)
                asyncio.get_running_loop().create_task(target.run())
            target.tokens.add(k)

    # ------------------------------------------------------------------ frames -> ticks

    def on_frame(self, buf: bytes) -> None:
        now = time.time()
        self.raw_frames.append(buf.hex())
        del self.raw_frames[:-40]
        out = []
        for p in wire.parse_market_frame(buf):
            if p.get("tc") is None:
                self.decode_errors += 1
                continue
            if p["tc"] not in (wire.TC_LTP, wire.TC_FULL, wire.TC_CHAIN) or "ltp" not in p:
                continue
            key = (wire.EXCHANGE_CODES.get(p["exchange"], 0), int(p["token"]))
            info = self.want.get(key)
            if info is None or p["ltp"] <= 0:
                continue
            self.raw_updates += 1
            self.trace.setdefault(info["instrument_id"], []).append((round(now, 2), p["tc"], p["ltp"], p.get("bid"), p.get("ask"), p.get("close")))
            del self.trace[info["instrument_id"]][:-200]
            recent = self.recent.setdefault(key, [])
            recent.append(int(p["ltp"]))
            del recent[: -self.median]
            ltp = sorted(recent)[len(recent) // 2]
            if ltp != int(p["ltp"]):
                self.suppressed += 1
            last = self.last.get(key, {})
            last = {**last, **{k: v for k, v in p.items() if v is not None}, "ltp": ltp, "raw_ltp": int(p["ltp"])}
            self.last[key] = last
            out.append((key, info, last))
            if last.get("close") and self.closes_saved.get(info["instrument_id"]) != last["close"]:
                self.closes_saved[info["instrument_id"]] = last["close"]
                threading.Thread(target=self._save_close, args=(info["instrument_id"], int(last["close"])), daemon=True).start()
        self._publish(out, now)

    def _save_close(self, iid: str, close: int) -> None:
        try:
            with session_scope() as s:
                s.execute(text("UPDATE instruments SET prev_close_paise=:c WHERE instrument_id=:i AND prev_close_paise<>:c"), {"c": close, "i": iid})
        except Exception:  # noqa: BLE001
            self.closes_saved.pop(iid, None)

    def _publish(self, items: list[tuple[tuple[int, int], dict, dict]], now: float) -> None:
        if not items or not self.publish_ticks:
            return
        c = events.client()
        pipe = c.pipeline(transaction=False)
        sec = int(now)
        ts = datetime.now(UTC)
        n = 0
        for key, info, last in items:
            if info["instrument_id"] in self.overrides:
                continue
            seq = self.seq.get(key, 0) + 1
            self.seq[key] = seq
            t = tick_payload(info, last, seq, ts)
            raw = json.dumps(t)
            pipe.set(events.TICK_KEY.format(info["instrument_id"]), raw)
            if self.hist_sec.get(key) != sec:
                self.hist_sec[key] = sec
                hk = events.HISTORY_KEY.format(info["instrument_id"])
                pipe.rpush(hk, json.dumps({"t": sec, "p": t["ltp_paise"]}))
                pipe.ltrim(hk, -900, -1)
            if not self.isolated:
                pipe.publish(events.CH_TICKS, raw)
            self.published_at[key] = now
            n += 1
        if n:
            pipe.execute()
            self.ticks_published += n

    def restamp(self) -> None:
        now = time.time()
        items = []
        for conn in self.conns:
            if not conn.alive:
                continue
            for key in conn.subscribed:
                last = self.last.get(key)
                info = self.want.get(key)
                if last and info and now - self.published_at.get(key, 0) >= 0.9:
                    items.append((key, info, last))
        self._publish(items, now)

    def refresh_overrides(self) -> None:
        try:
            self.overrides = {k[len(OVERRIDE_PREFIX) :] for k in events.client().scan_iter(OVERRIDE_PREFIX + "*", count=200)}
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ loop

    def drain_requests(self) -> int:
        """Instruments other processes asked for (market.latest found no tick): see core/market.py WANT_021_KEY."""
        try:
            ids = events.client().spop(WANT_021_KEY, 200) or []
        except Exception:  # noqa: BLE001
            return 0
        return self.request(list(ids)) if ids else 0

    async def main(self) -> None:
        last_want = 0.0
        while True:
            try:
                if await asyncio.to_thread(self.drain_requests) or self.dirty:
                    self.dirty = False
                    last_want = 0.0
                if time.time() - last_want > 5.0:
                    last_want = time.time()
                    want = await asyncio.to_thread(self.compute_want)
                    self.assign(want)
                await asyncio.to_thread(self.refresh_overrides)
                self.restamp()
            except Exception as e:  # noqa: BLE001
                print("gateway021 market loop error:", type(e).__name__, str(e)[:200])
            await asyncio.sleep(1.0)

    def run_forever(self) -> None:
        asyncio.run(self.main())

    def status(self) -> dict:
        return {
            "connections": len(self.conns),
            "alive": sum(1 for c in self.conns if c.alive),
            "subscribed": sum(len(c.subscribed) for c in self.conns),
            "wanted": len(self.want),
            "frames": sum(c.frames for c in self.conns),
            "reconnects": sum(max(0, c.connects - 1) for c in self.conns),
            "ticks_published": self.ticks_published,
            "decode_errors": self.decode_errors,
            "raw_updates": self.raw_updates,
            "median_window": self.median,
            "median_differs": self.suppressed,
            "last_errors": [c.last_error for c in self.conns if c.last_error][:3],
            "publishing": self.publish_ticks,
            "overrides": sorted(self.overrides),
        }

    def latest(self, instrument_id: str) -> dict | None:
        for key, info in self.want.items():
            if info["instrument_id"] == instrument_id and key in self.last:
                return self.last[key]
        return None
