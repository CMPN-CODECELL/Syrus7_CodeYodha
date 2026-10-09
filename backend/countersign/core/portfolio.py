"""Read-only account and market views (P&L, holdings, positions, orders, quotes, option chain).
Uses only the ReadGateway (broker READ key) and the tick cache. Instrument names are sanitized display names."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from countersign.core import instruments, market
from countersign.core.broker_client import ReadGateway
from countersign.core.models import Instrument
from countersign.core.schemas import iso


def _handle(s: Session, iid: str) -> dict:
    inst = s.get(Instrument, iid)
    if inst is None:
        return {"instrument_id": iid, "exchange": "NSE", "segment": "EQ", "symbol": iid.split(":")[-1], "display_name": iid, "lot_size": 1, "tick_size_paise": 5, "flags": []}
    return instruments.handle(inst).model_dump(mode="json")


def holdings(s: Session, gw: ReadGateway) -> list[dict]:
    rows = gw.holdings()
    ticks = market.latest_many([r["instrument_id"] for r in rows])
    out = []
    for r in rows:
        inst = s.get(Instrument, r["instrument_id"])
        t = ticks.get(r["instrument_id"])
        ltp = t.ltp_paise if t else (inst.prev_close_paise if inst else r["avg_price_paise"])
        prev = inst.prev_close_paise if inst else ltp
        q = int(r["quantity"])
        avg = int(r["avg_price_paise"])
        pnl = (ltp - avg) * q
        out.append(
            {
                "instrument": _handle(s, r["instrument_id"]),
                "quantity": q,
                "avg_price_paise": avg,
                "ltp_paise": ltp,
                "prev_close_paise": prev,
                "value_paise": ltp * q,
                "pnl_paise": pnl,
                "pnl_bps": int((ltp - avg) * 10000 / avg) if avg else 0,
                "day_change_paise": (ltp - prev) * q,
                "day_change_bps": int((ltp - prev) * 10000 / prev) if prev else 0,
            }
        )
    out.sort(key=lambda h: h["value_paise"], reverse=True)
    return out


def positions(s: Session, gw: ReadGateway) -> list[dict]:
    rows = gw.positions()
    ticks = market.latest_many([r["instrument_id"] for r in rows])
    out = []
    for r in rows:
        t = ticks.get(r["instrument_id"])
        inst = s.get(Instrument, r["instrument_id"])
        ltp = t.ltp_paise if t else r["avg_price_paise"]
        q = int(r["quantity"])
        avg = int(r["avg_price_paise"])
        unreal = (ltp - avg) * q
        prev = inst.prev_close_paise if inst else avg
        out.append(
            {
                "instrument": _handle(s, r["instrument_id"]),
                "product": r["product"],
                "quantity": q,
                "avg_price_paise": avg,
                "ltp_paise": ltp,
                "pnl_paise": unreal + int(r.get("realised_paise", 0)),
                "pnl_bps": int((ltp - avg) * 10000 / avg) * (1 if q >= 0 else -1) if avg else 0,
                "day_pnl_paise": (ltp - avg) * q,
            }
        )
    return out


def orders(s: Session, gw: ReadGateway, status: str = "all") -> list[dict]:
    out = []
    for o in gw.orders(status=status):
        o = dict(o)
        o["instrument"] = _handle(s, o["instrument_id"])
        out.append(o)
    return out


def summary(s: Session, gw: ReadGateway, account_id: str) -> dict:
    f = gw.funds()
    hs = holdings(s, gw)
    ps = positions(s, gw)
    open_orders = gw.orders(status="open")
    day = sum(h["day_change_paise"] for h in hs) + sum(p["day_pnl_paise"] for p in ps)
    total = sum(h["pnl_paise"] for h in hs) + sum(p["pnl_paise"] for p in ps)
    hv = sum(h["value_paise"] for h in hs)
    exposure = hv + sum(abs(p["quantity"]) * p["ltp_paise"] for p in ps)
    return {
        "account_id": account_id,
        "day_pnl_paise": day,
        "total_pnl_paise": total,
        "cash_paise": f["cash_paise"],
        "available_margin_paise": f["available_paise"],
        "used_margin_paise": f["used_margin_paise"],
        "holdings_value_paise": hv,
        "exposure_paise": exposure,
        "holdings_count": len(hs),
        "positions_count": len(ps),
        "open_orders_count": len(open_orders),
        "as_of": iso(datetime.now(UTC)),
    }


def quote(s: Session, ref: str) -> dict | None:
    r = instruments.resolve(s, ref)
    if r.status != "RESOLVED":
        return None
    t = market.latest(r.instrument.instrument_id)
    if t is None:
        return None
    return {"instrument": instruments.handle(r.instrument).model_dump(mode="json"), "tick": t.to_api()}


def option_chain(s: Session, expiry: str | None = None, width: int = 8) -> dict:
    exps = instruments.nifty_expiries()
    exp = expiry if expiry in exps else exps[0]
    spot_t = market.latest("NSE:IDX:NIFTY")
    spot = spot_t.ltp_paise if spot_t else s.get(Instrument, "NSE:IDX:NIFTY").prev_close_paise
    step = instruments.NIFTY_STEP_PAISE
    atm = round(spot / step) * step
    rows = []
    insts = {i.instrument_id: i for i in s.execute(select(Instrument).where(Instrument.underlying == "NIFTY", Instrument.expiry == exp)).scalars()}
    ids = list(insts.keys())
    ticks = market.latest_many(ids)
    from countersign.core.pricing import implied_vol, option_premium_paise

    for k in range(-width, width + 1):
        strike = atm + k * step
        legs = {}
        for opt in ("CE", "PE"):
            iid = instruments.option_id(exp, strike // 100, opt)
            inst = insts.get(iid)
            t = ticks.get(iid)
            ltp = t.ltp_paise if t else option_premium_paise(spot, strike, opt, exp)
            prev = inst.prev_close_paise if inst else ltp
            oi_base = 4_000_000 // (1 + abs(k)) + (3000 * (12 - abs(k)) if abs(k) < 12 else 0)
            legs[opt] = {
                "instrument_id": iid,
                "ltp_paise": ltp,
                "oi": oi_base * (12 if opt == "PE" and k < 0 else 10),
                "volume": t.volume if t else 0,
                "iv_bps": int(implied_vol(spot / 100, strike / 100) * 10000),
                "change_bps": int((ltp - prev) * 10000 / prev) if prev else 0,
            }
        rows.append({"strike_paise": strike, "ce": legs["CE"], "pe": legs["PE"], "is_atm": k == 0, "distance": k})
    return {
        "underlying": "NIFTY",
        "spot_paise": spot,
        "expiry": exp,
        "expiries": exps,
        "atm_strike_paise": atm,
        "strike_step_paise": step,
        "lot_size": instruments.NIFTY_LOT,
        "rows": rows,
        "as_of": iso(datetime.now(UTC)),
    }


def watchlist(s: Session) -> list[dict]:
    rows = s.execute(select(Instrument).where(Instrument.segment.in_(["EQ", "IDX"]), Instrument.status == "ACTIVE")).scalars().all()
    rows = [r for r in rows if instruments.is_ascii_symbol(r.symbol) and "LOOKALIKE_SYMBOL" not in (r.flags or [])]
    ticks = market.latest_many([r.instrument_id for r in rows])
    out = []
    for r in sorted(rows, key=lambda x: (x.segment != "IDX", x.symbol)):
        t = ticks.get(r.instrument_id)
        if t:
            out.append({"instrument": instruments.handle(r).model_dump(mode="json"), "tick": t.to_api()})
    return out
