"""Read-only account and market views (P&L, holdings, positions, orders, quotes, option chain).
Uses only the ReadGateway (broker READ key) and the tick cache. Instrument names are sanitized display names."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from fractions import Fraction

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
        # Previous close: the broker's own figure when it reports one (021 holdings carry prevClose), else the feed's.
        prev = int(r.get("prev_close_paise") or 0) or (t.prev_close_paise if t else 0) or (inst.prev_close_paise if inst else ltp)
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
        prev = int(r.get("prev_close_paise") or 0) or (t.prev_close_paise if t else 0) or (inst.prev_close_paise if inst else avg)
        out.append(
            {
                "instrument": _handle(s, r["instrument_id"]),
                "product": "MIS" if str(r["product"]).upper() == "INTRADAY" else r["product"],
                "quantity": q,
                "avg_price_paise": avg,
                "ltp_paise": ltp,
                "pnl_paise": unreal + int(r.get("realised_paise", 0)),
                "unrealised_pnl_paise": unreal,
                "pnl_bps": int((ltp - avg) * 10000 / avg) * (1 if q >= 0 else -1) if avg else 0,
                "day_pnl_paise": (ltp - avg) * q,
                "day_change_bps": int((ltp - prev) * 10000 / prev) * (1 if q >= 0 else -1) if prev else 0,
            }
        )
    return out


def losing_positions(rows: list[dict], threshold_pct: float, basis: str = "avg") -> list[dict]:
    """Open positions down at least threshold_pct: from the average price (basis avg) or from the previous close
    (basis day), signed for the position's direction (a short loses when the price rises)."""
    key = "day_change_bps" if basis == "day" else "pnl_bps"
    return [p for p in rows if p["quantity"] != 0 and p[key] <= -int(threshold_pct * 100)]


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


def _ceil_lot(x: Fraction | int, lot: int) -> int:
    n = math.ceil(x)
    return -(-n // lot) * lot if lot > 1 else n


def rebalance_sells(rows: list[dict], max_bps: int, max_rounds: int = 5000) -> dict:
    """Deterministic sells-only rebalance so that no holding is above max_bps of the holdings value that remains.

    rows: [{key, qty, price (paise), lot}]. Weight of k = qty_k x price_k / V, V = sum over all holdings.
    Formula: with C the set of capped stocks and L the cap, each capped stock ends at value cap = L x U / (1 - |C| x L),
    U being the value of the uncapped stocks (water-filling: a stock enters C while its value exceeds cap). Sells are
    then ceil((value_k - cap) / price_k) rounded UP to the lot so the stock ends at or below the cap, followed by an
    exact integer check that tops up the most overweight stock until every weight is within L.
    Returns status WITHIN | PLAN | INFEASIBLE, sells {key: qty}, before/after weights in bps (Fractions), values."""
    L = Fraction(max_bps, 10000)
    qty = {r["key"]: int(r["qty"]) for r in rows if int(r["qty"]) > 0}
    price = {r["key"]: int(r["price"]) for r in rows}
    lot = {r["key"]: max(int(r.get("lot") or 1), 1) for r in rows}

    def value(q: dict[str, int]) -> int:
        return sum(q[k] * price[k] for k in q)

    v0 = value(qty)
    out: dict = {"status": "INFEASIBLE", "sells": {}, "before": {}, "after": {}, "value_before": v0, "value_after": v0, "reason": None}
    if v0 <= 0:
        out["reason"] = "Your holdings have no market value to rebalance."
        return out
    out["before"] = {k: Fraction(qty[k] * price[k] * 10000, v0) for k in qty}
    if all(qty[k] * price[k] * 10000 <= max_bps * v0 for k in qty):
        out.update(status="WITHIN", after=dict(out["before"]))
        return out
    n = len(qty)
    if n * L < 1:
        out["reason"] = f"You hold {n} stock(s). With sells only, {n} x {max_bps / 100:g}% is less than 100%, so at least one stock must stay above {max_bps / 100:g}% unless everything is sold."
        return out
    vals = {k: Fraction(qty[k] * price[k]) for k in qty}
    capped: set[str] = set()
    while True:
        denom = 1 - len(capped) * L
        if denom <= 0:
            out["reason"] = f"The {max_bps / 100:g}% cap cannot be met with sells only for these holdings."
            return out
        cap = L * sum(v for k, v in vals.items() if k not in capped) / denom
        new = {k for k in vals if k not in capped and vals[k] > cap}
        if not new:
            break
        capped |= new
    sells = {k: min(_ceil_lot((vals[k] - cap) / price[k], lot[k]), qty[k]) for k in capped}
    for _ in range(max_rounds):
        rem = {k: qty[k] - sells.get(k, 0) for k in qty}
        v = value(rem)
        if v <= 0:
            out["reason"] = f"Meeting the {max_bps / 100:g}% cap would mean selling everything."
            return out
        over = [k for k in rem if rem[k] * price[k] * 10000 > max_bps * v]
        if not over:
            break
        k = max(over, key=lambda x: rem[x] * price[x])
        need = Fraction(rem[k] * price[k] * 10000 - max_bps * v, price[k] * (10000 - max_bps))
        sells[k] = min(sells.get(k, 0) + max(_ceil_lot(need, lot[k]), lot[k]), qty[k])
    else:
        out["reason"] = f"Whole-share quantities cannot meet the {max_bps / 100:g}% cap exactly for these holdings."
        return out
    rem = {k: qty[k] - sells.get(k, 0) for k in qty}
    v = value(rem)
    out.update(
        status="PLAN",
        sells={k: s for k, s in sells.items() if s > 0},
        after={k: Fraction(rem[k] * price[k] * 10000, v) for k in rem},
        value_after=v,
    )
    return out
