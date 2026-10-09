"""021 orders websocket: live order events for this account. Each event only says WHICH order changed; the gateway then
reads that order from REST (the source of truth) and publishes it in the paper broker's shape on the broker-updates
channel, so executor/tracker.py applies it exactly like a paper broker postback. Events that happen while the socket is
down are not replayed by 021, so every (re)connect starts with a full catch-up from GET /orders."""

from __future__ import annotations

import asyncio
import random
import threading
import time

from countersign.broker021 import wire
from countersign.broker021.wire import redact


class OrdersSocket:
    def __init__(self, gw, ws_base: str):
        self.gw = gw
        self.ws_base = ws_base
        self.connected = False
        self.events = 0
        self.connects = 0
        self.last_event: dict | None = None
        self.last_error: str | None = None
        self.decode_errors = 0
        self.raw: list[tuple[int, str]] = []

    def _handle(self, buf: bytes) -> None:
        # Diagnostics keep the event bytes with the account code (bytes 4..13) blanked out.
        self.raw.append((len(buf), (buf[:4] + bytes(10) + buf[14:]).hex() if len(buf) >= 14 else buf.hex()))
        del self.raw[:-20]
        try:
            ev = wire.parse_order_event(buf)
        except wire.FrameError as e:
            self.decode_errors += 1
            self.last_error = str(e)
            return
        if ev.get("event") == "HEARTBEAT":
            return
        self.events += 1
        # Only the event kind and the order id are kept (the UCC field is never logged or exposed).
        self.last_event = {"event": ev["event"], "order_id": ev["order_id"], "at": time.time()}
        threading.Thread(target=self.gw.refresh_one, args=(str(ev["order_id"]),), daemon=True).start()

    async def run(self) -> None:
        import websockets

        backoff = 1.0
        while True:
            try:
                data = await asyncio.to_thread(self.gw.client.get, "/websocket/ephemeral-key")
                key = ((data or {}).get("data") or {}).get("token")
                if not key:
                    raise RuntimeError("021 returned no ephemeral key")
                async with websockets.connect(f"{self.ws_base}/orders?token={key}", max_size=2**20, open_timeout=15, ping_interval=20, ping_timeout=20) as ws:
                    self.connected = True
                    self.connects += 1
                    backoff = 1.0
                    await asyncio.to_thread(self.gw.sync)  # catch up: nothing missed while disconnected is replayed
                    async for msg in ws:
                        if isinstance(msg, (bytes, bytearray)):
                            self._handle(bytes(msg))
                self.last_error = "socket closed by the server"
            except Exception as e:  # noqa: BLE001
                self.last_error = redact(f"{type(e).__name__}: {str(e)[:160]}")
            finally:
                self.connected = False
            await asyncio.sleep(backoff + random.uniform(0, backoff))
            backoff = min(backoff * 2, 30.0)

    def run_forever(self) -> None:
        asyncio.run(self.run())

    def status(self) -> dict:
        return {"connected": self.connected, "events": self.events, "connects": self.connects, "last_event": self.last_event, "last_error": self.last_error, "decode_errors": self.decode_errors, "recent_raw_masked": self.raw[-5:]}
