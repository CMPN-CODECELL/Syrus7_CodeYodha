"""Binary websocket formats of the 021 sandbox (big-endian, prices in paise).

Market socket: one frame carries many packets back to back; each starts with a 2-byte transaction code (TC).
  TC1  LTP (ltp/ltpo/ltpc), 12 bytes; in snapshot frames ltpo/ltpc packets carry 5 extra bytes ('o' or 'c' + price)
  TC3  full packet; its length depends on the exchange code at offset 2 (NSECM 220, NSEFO 234, NSEIDX 36, BSE 262)
  TC9  option-chain packet with a variable list of (id, value) fields
  TC10 heartbeat, 2 bytes
Orders socket: one event per frame. TC4 NSE (46 bytes + optional reason), TC8 BSE (32 bytes + optional reason)."""

from __future__ import annotations

import re
import struct

EXCHANGES = {1: "NSECM", 2: "NSEFO", 3: "NSEIDX", 4: "BSEEQ", 5: "BSEEQD", 6: "BSEIDX"}
EXCHANGE_CODES = {v: k for k, v in EXCHANGES.items()}
FULL_LENGTHS = {1: 220, 2: 234, 3: 36, 4: 262, 5: 262, 6: 262}
TC_LTP, TC_FULL, TC_ORDER_NSE, TC_ORDER_BSE, TC_CHAIN, TC_HEARTBEAT = 1, 3, 4, 8, 9, 10
# TC9 field ids: (name, size in bytes)
CHAIN_FIELDS = {1: ("ltp", 4), 2: ("oi", 8), 3: ("volume", 8), 4: ("iv", 4), 7: ("depth", 32), 8: ("greeks", 20), 9: ("pcr_oi", 4)}
ORDER_STATUS = {
    1: "TRADE",
    2: "ACCEPTED",
    3: "REJECTED",
    4: "SL_TRIGGERED",
    5: "MODIFY_ACCEPTED",
    6: "MODIFY_REJECTED",
    7: "CANCELLED",
    8: "CANCEL_REJECTED",
    9: "PRICE_CONFIRMATION",
}


class FrameError(ValueError):
    """A packet could not be decoded; the rest of the frame is dropped (never guessed)."""


def _depth(buf: bytes, off: int, level_size: int) -> tuple[int, int, int, int]:
    """Best bid (level 0) and best ask (level 5) of a 10-level depth block: (bid, bid_qty, ask, ask_qty)."""
    bid_qty, bid = struct.unpack_from(">qi", buf, off)
    ask_qty, ask = struct.unpack_from(">qi", buf, off + 5 * level_size)
    return bid, bid_qty, ask, ask_qty


def _full(buf: bytes, i: int, exch: int) -> dict:
    if exch == 1:
        token, ltp, ltq, avg, vol = struct.unpack_from(">iiiiq", buf, i + 4)
        close, open_, high, low = struct.unpack_from(">iiii", buf, i + 44)
        bid, bid_qty, ask, ask_qty = _depth(buf, i + 64, 14)
        return {"token": token, "ltp": ltp, "last_qty": ltq, "avg_price": avg, "volume": vol, "close": close, "open": open_, "high": high, "low": low, "bid": bid, "bid_qty": bid_qty, "ask": ask, "ask_qty": ask_qty}
    if exch == 2:
        token, ltp, ltq, avg, vol = struct.unpack_from(">iiiiq", buf, i + 6)
        close, open_, high, low = struct.unpack_from(">iiii", buf, i + 46)
        bid, bid_qty, ask, ask_qty = _depth(buf, i + 66, 14)
        (oi,) = struct.unpack_from(">q", buf, i + 206)
        return {"token": token, "ltp": ltp, "last_qty": ltq, "avg_price": avg, "volume": vol, "close": close, "open": open_, "high": high, "low": low, "bid": bid, "bid_qty": bid_qty, "ask": ask, "ask_qty": ask_qty, "oi": oi}
    if exch == 3:
        token, value, close, open_, high, low = struct.unpack_from(">iiiiii", buf, i + 4)
        return {"token": token, "ltp": value, "close": close, "open": open_, "high": high, "low": low, "volume": 0}
    token, ltp = struct.unpack_from(">ii", buf, i + 4)
    (ltq,) = struct.unpack_from(">q", buf, i + 12)
    (avg,) = struct.unpack_from(">i", buf, i + 20)
    (vol,) = struct.unpack_from(">q", buf, i + 24)
    close, open_, high, low = struct.unpack_from(">iiii", buf, i + 48)
    bid, bid_qty, ask, ask_qty = _depth(buf, i + 68, 16)
    out = {"token": token, "ltp": ltp, "last_qty": ltq, "avg_price": avg, "volume": vol, "close": close, "open": open_, "high": high, "low": low, "bid": bid, "bid_qty": bid_qty, "ask": ask, "ask_qty": ask_qty}
    (out["oi"],) = struct.unpack_from(">q", buf, i + 228)
    return out


def parse_market_frame(buf: bytes) -> list[dict]:
    """Every packet in one market frame, in order. Each dict has tc, exchange (021 name), token and the fields of
    its layout. A truncated or unknown packet ends parsing of that frame (FrameError is not raised, the packets
    decoded so far are returned with a trailing {"tc": None, "error": ...} marker)."""
    out: list[dict] = []
    i, n = 0, len(buf)
    while i + 2 <= n:
        (tc,) = struct.unpack_from(">H", buf, i)
        try:
            if tc == TC_HEARTBEAT:
                out.append({"tc": TC_HEARTBEAT})
                i += 2
            elif tc == TC_LTP:
                if i + 12 > n:
                    raise FrameError("truncated LTP packet")
                exch, token, ltp = struct.unpack_from(">Hii", buf, i + 2)
                p = {"tc": TC_LTP, "exchange": EXCHANGES.get(exch, str(exch)), "token": token, "ltp": ltp}
                i += 12
                # Snapshot frames: ltpo / ltpc packets carry 'o' or 'c' plus a 4-byte price. A new packet always
                # starts with 0x00, so any other next byte means the extra bytes are present.
                if i < n and buf[i] != 0x00:
                    if i + 5 > n or buf[i] not in (0x6F, 0x63):
                        raise FrameError("unexpected bytes after an LTP packet")
                    (extra,) = struct.unpack_from(">i", buf, i + 1)
                    p["open" if buf[i] == 0x6F else "close"] = extra
                    i += 5
                out.append(p)
            elif tc == TC_FULL:
                if i + 4 > n:
                    raise FrameError("truncated full packet header")
                (exch,) = struct.unpack_from(">H", buf, i + 2)
                size = FULL_LENGTHS.get(exch)
                if size is None:
                    raise FrameError(f"unknown exchange code {exch} in a full packet")
                if i + size > n:
                    raise FrameError("truncated full packet")
                out.append({"tc": TC_FULL, "exchange": EXCHANGES[exch], **_full(buf, i, exch)})
                i += size
            elif tc == TC_CHAIN:
                if i + 9 > n:
                    raise FrameError("truncated option-chain packet")
                token, exch, nfields = struct.unpack_from(">iHB", buf, i + 2)
                p = {"tc": TC_CHAIN, "exchange": EXCHANGES.get(exch, str(exch)), "token": token}
                j = i + 9
                for _ in range(nfields):
                    if j >= n:
                        raise FrameError("truncated option-chain field")
                    fid = buf[j]
                    if fid not in CHAIN_FIELDS:
                        raise FrameError(f"unknown option-chain field id {fid}")
                    name, size = CHAIN_FIELDS[fid]
                    if j + 1 + size > n:
                        raise FrameError("truncated option-chain value")
                    raw = buf[j + 1 : j + 1 + size]
                    if name in ("ltp", "iv", "pcr_oi"):
                        p[name] = struct.unpack(">i", raw)[0]
                    elif name in ("oi", "volume"):
                        p[name] = struct.unpack(">q", raw)[0]
                    elif name == "depth":
                        p["bid"], p["ask"], p["bid_qty"], p["ask_qty"] = struct.unpack(">qqqq", raw)
                    else:
                        p["delta"], p["gamma"], p["vega"], p["theta"], p["rho"] = struct.unpack(">iiiii", raw)
                    j += 1 + size
                out.append(p)
                i = j
            else:
                raise FrameError(f"unknown transaction code {tc}")
        except (FrameError, struct.error) as e:
            out.append({"tc": None, "error": str(e), "offset": i})
            break
    return out


def _ascii(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


def parse_order_event(buf: bytes) -> dict:
    """One orders-socket event. Raises FrameError for an unknown or truncated packet."""
    if len(buf) >= 2 and struct.unpack_from(">H", buf, 0)[0] == TC_HEARTBEAT:
        return {"tc": TC_HEARTBEAT, "event": "HEARTBEAT"}
    if len(buf) < 4:
        raise FrameError("truncated order event")
    tc, status = struct.unpack_from(">HH", buf, 0)
    if tc == TC_ORDER_NSE:
        if len(buf) < 46:
            raise FrameError("truncated NSE order event")
        order_id, qty, price = struct.unpack_from(">iii", buf, 34)
        return {
            "tc": tc,
            "status": status,
            "event": ORDER_STATUS.get(status, f"UNKNOWN_{status}"),
            "ucc": _ascii(buf[4:14]),
            "symbol": _ascii(buf[14:34]),
            "exchange": None,
            "token": None,
            "order_id": order_id,
            "qty": qty,
            "price": price,
            "reason": _ascii(buf[46:]) or None,
        }
    if tc == TC_ORDER_BSE:
        if len(buf) < 32:
            raise FrameError("truncated BSE order event")
        exch, token, order_id, qty, price = struct.unpack_from(">Hiiii", buf, 14)
        return {
            "tc": tc,
            "status": status,
            "event": ORDER_STATUS.get(status, f"UNKNOWN_{status}"),
            "ucc": _ascii(buf[4:14]),
            "symbol": None,
            "exchange": EXCHANGES.get(exch, str(exch)),
            "token": token,
            "order_id": order_id,
            "qty": qty,
            "price": price,
            "reason": _ascii(buf[32:]) or None,
        }
    raise FrameError(f"unknown order event code {tc}")


def redact(text: str) -> str:
    """Error texts may echo the websocket URL; its ephemeral key never leaves the gateway."""
    return re.sub(r"(token=)[^&\s'\"]+", lambda m: m.group(1) + "***", text)
