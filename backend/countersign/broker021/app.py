"""gateway021: the ONLY process that holds the 021 UCC, password and access token.

It exposes the paper broker's /v1 interface and key scopes (read, write, admin, audit; the naive key is refused), so
ReadGateway, OrderExecutionProvider and BrokerAdmin reach the real 021 sandbox by base URL alone. It serves exactly
one 021 account (the configured UCC); X-Account must name it.

Run: python -m countersign.broker021.app   (port 8200; started by scripts/dev.py when CS_021_UCC is configured)"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from countersign.broker021 import instruments021, store
from countersign.broker021.gateway import Gateway, GatewayError
from countersign.broker021.settings import gateway_settings
from countersign.core import events
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import Instrument, Instrument021

STATUS_KEY = "cs:gw021:status"

app = FastAPI(title="Countersign 021 gateway")
_gw: Gateway | None = None
_md = None
_ows = None
_boot: dict = {"instruments": None, "recovered": [], "started_at": None, "error": None}
_faults = {"drop_after_accept": 0}


def gw() -> Gateway:
    global _gw
    if _gw is None:
        cfg = gateway_settings()
        _gw = Gateway(cfg, publish=(lambda order: None) if cfg.isolated else None)
    return _gw


def mask(ucc: str) -> str:
    return (ucc[:4] + "***" + ucc[-1:]) if len(ucc) > 5 else "***"


# ------------------------------------------------------------------ auth (same keys and scopes as the paper broker)


class Caller(BaseModel):
    scope: str


def _key_scopes() -> dict[str, str]:
    cfg = settings()
    out = {}
    for key, scope in ((cfg.broker_read_key, "read"), (cfg.broker_write_key, "write"), (cfg.broker_admin_key, "admin"), (cfg.broker_audit_key, "audit")):
        if key:
            out[hashlib.sha256(key.encode()).hexdigest()] = scope
    return out


def caller(authorization: str = Header(default=""), x_account: str = Header(default="")) -> Caller:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Missing broker API key."})
    presented = hashlib.sha256(authorization[7:].encode()).hexdigest()
    scope = next((sc for h, sc in _key_scopes().items() if hmac.compare_digest(h, presented)), None)
    if scope is None:
        # The naive copilot's key is never registered here: the unsafe baseline can never reach a real account.
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Invalid broker API key for the 021 gateway."})
    if scope in ("read", "write") and x_account and x_account != gw().account_id:
        raise HTTPException(403, {"code": "ACCOUNT_FORBIDDEN", "message": "This gateway serves a different 021 account."})
    return Caller(scope=scope)


def need(*scopes: str):
    def dep(c: Caller = Depends(caller)) -> Caller:
        if c.scope not in scopes:
            raise HTTPException(403, {"code": "SCOPE_FORBIDDEN", "message": f"Key scope '{c.scope}' cannot perform this operation."})
        return c

    return dep


def _account_required(x_account: str = Header(default="")) -> None:
    if not x_account:
        raise HTTPException(400, {"code": "ACCOUNT_REQUIRED", "message": "Send X-Account with the 021 account id."})


READ = Depends(need("read", "write", "audit"))


@app.exception_handler(HTTPException)
def _http_exc(_: Request, exc: HTTPException):
    d = exc.detail if isinstance(exc.detail, dict) else {"code": "ERROR", "message": str(exc.detail)}
    return JSONResponse(d, status_code=exc.status_code)


@app.exception_handler(GatewayError)
def _gw_exc(_: Request, exc: GatewayError):
    return JSONResponse(exc.body(), status_code=exc.status)


# ------------------------------------------------------------------ lifecycle


def _status() -> dict:
    g = gw()
    st = g.client.stats
    return {
        "ok": g.client.logged_in or _boot["error"] is None,
        "broker": "021",
        "account": mask(g.account_id),
        "logged_in": g.client.logged_in,
        "token_expires_at": datetime.fromtimestamp(g.client.token_expires_at, UTC).isoformat().replace("+00:00", "Z") if g.client.token_expires_at else None,
        "clock_offset_s": round(g.client.clock_offset_s, 2),
        "instruments": _boot["instruments"],
        "market": _md.status() if _md else {"enabled": False},
        "orders_socket": _ows.status() if _ows else {"enabled": False},
        "http": {"logins": st.logins, "relogins_on_401": st.relogins_on_401, "read_retries": st.read_retries, "http_429": st.http_429, "http_5xx": st.http_5xx, "timeouts": st.timeouts, "by_status": {str(k): v for k, v in st.by_status.items()}, "last_error": st.last_error},
        "last_reconciliation": g.last_reconciliation,
        "faults": dict(_faults) if gateway_settings().test_faults else None,
        "boot_error": _boot["error"],
        "started_at": _boot["started_at"],
        "ts": time.time(),
    }


def _heartbeat() -> None:
    while True:
        try:
            events.client().set(STATUS_KEY, json.dumps(_status(), default=str), ex=30)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.0)


def _poller() -> None:
    first = True
    while True:
        try:
            gw().sync(publish_all=first)
            first = False
        except Exception as e:  # noqa: BLE001
            _boot["poll_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        time.sleep(3.0)


def _daily_instruments() -> None:
    while True:
        time.sleep(600)
        try:
            ist = datetime.now(UTC).timestamp() + 19800
            hour = int(ist // 3600) % 24
            if (_boot["instruments"] or {}).get("date") != instruments021.ist_today() and hour >= 8:
                _boot["instruments"] = instruments021.ensure_loaded(gw().client, gateway_settings().instruments_cache)
                gw().lookup.refresh()
        except Exception as e:  # noqa: BLE001
            _boot["error"] = f"daily instrument reload failed: {type(e).__name__}: {str(e)[:200]}"


@app.on_event("startup")
def _startup() -> None:
    global _md, _ows
    cfg = gateway_settings()
    _boot["started_at"] = time.time()
    if not cfg.configured:
        _boot["error"] = "CS_021_UCC / CS_021_PASSWORD are not configured; the gateway cannot reach 021."
        return
    from countersign.core.models import create_all

    create_all()
    store.init_db()
    try:
        _boot["instruments"] = instruments021.ensure_loaded(gw().client, cfg.instruments_cache)
        gw().lookup.refresh()
    except Exception as e:  # noqa: BLE001
        _boot["error"] = f"instrument load failed: {type(e).__name__}: {str(e)[:200]}"
    try:
        _boot["recovered"] = gw().recover()
    except Exception as e:  # noqa: BLE001
        _boot["error"] = f"startup reconciliation failed: {type(e).__name__}: {str(e)[:200]}"
    gw().client.drop_next_write_responses = 0
    threading.Thread(target=_poller, daemon=True, name="g021-poller").start()
    threading.Thread(target=_heartbeat, daemon=True, name="g021-heartbeat").start()
    threading.Thread(target=_daily_instruments, daemon=True, name="g021-instruments").start()
    if cfg.market_ws:
        from countersign.broker021.marketdata import MarketData

        _md = MarketData(gw(), cfg.ws_base, publish_ticks=cfg.publish_ticks, isolated=cfg.isolated, median=cfg.tick_median)
        threading.Thread(target=_md.run_forever, daemon=True, name="g021-market").start()
    if cfg.orders_ws:
        from countersign.broker021.orders_ws import OrdersSocket

        _ows = OrdersSocket(gw(), cfg.ws_base)
        threading.Thread(target=_ows.run_forever, daemon=True, name="g021-orders").start()


@app.get("/health")
def health() -> dict:
    return _status()


# ------------------------------------------------------------------ reads


@app.get("/v1/account")
def account(c: Caller = Depends(need("read", "write", "admin", "audit"))) -> dict:
    return {"account_id": gw().account_id, "broker": "021", "label": mask(gw().account_id)}


@app.get("/v1/funds", dependencies=[Depends(_account_required)])
def funds(c: Caller = READ) -> dict:
    return gw().funds()


@app.get("/v1/holdings", dependencies=[Depends(_account_required)])
def holdings(c: Caller = READ) -> list[dict]:
    return gw().holdings()


@app.get("/v1/positions", dependencies=[Depends(_account_required)])
def positions(c: Caller = READ) -> list[dict]:
    return gw().positions()


@app.get("/v1/orders", dependencies=[Depends(_account_required)])
def orders(status: str = "all", tag: str | None = None, c: Caller = READ) -> list[dict]:
    return gw().orders(status=status, tag=tag)


@app.get("/v1/orders/{oid}", dependencies=[Depends(_account_required)])
def order(oid: str, c: Caller = READ) -> dict:
    return gw().order(oid)


@app.get("/v1/trades", dependencies=[Depends(_account_required)])
def trades(c: Caller = READ) -> list[dict]:
    return gw().trades()


@app.get("/v1/attempts/{tag}", dependencies=[Depends(_account_required)])
def attempt(tag: str, wait_s: float = 0.0, c: Caller = READ) -> dict:
    a = gw().attempt(tag, wait_s=max(0.0, min(wait_s, 25.0)))
    if a is None:
        raise HTTPException(404, {"code": "TAG_UNKNOWN", "message": f"The gateway never received an order with tag {tag}, so nothing was sent to 021 for it."})
    return a


# ------------------------------------------------------------------ writes (write key only; never retried)


class PlaceBody(BaseModel):
    instrument_id: str
    symbol: str
    segment: str = "EQ"
    side: str
    quantity: int
    order_type: str
    product: str
    validity: str = "DAY"
    limit_price_paise: int | None = None
    trigger_price_paise: int | None = None
    protection_price_paise: int | None = None
    tag: str | None = None


@app.post("/v1/orders", dependencies=[Depends(_account_required)])
def place(body: PlaceBody, c: Caller = Depends(need("write"))) -> dict:
    g = gw()
    if _faults["drop_after_accept"] > 0:
        _faults["drop_after_accept"] -= 1
        g.client.drop_next_write_responses = 1
    try:
        return g.place(body.model_dump())
    finally:
        g.client.drop_next_write_responses = 0


class ModifyBody(BaseModel):
    quantity: int | None = None
    limit_price_paise: int | None = None
    trigger_price_paise: int | None = None


@app.put("/v1/orders/{oid}", dependencies=[Depends(_account_required)])
def modify(oid: str, body: ModifyBody, c: Caller = Depends(need("write"))) -> dict:
    return gw().modify(oid, body.quantity, body.limit_price_paise, body.trigger_price_paise)


@app.delete("/v1/orders/{oid}", dependencies=[Depends(_account_required)])
def cancel(oid: str, c: Caller = Depends(need("write"))) -> dict:
    return gw().cancel(oid)


# ------------------------------------------------------------------ market data


class SubscribeBody(BaseModel):
    instrument_ids: list[str]


@app.post("/v1/market/subscribe")
def subscribe(body: SubscribeBody, c: Caller = READ) -> dict:
    if _md is None:
        raise HTTPException(503, {"code": "MARKET_DATA_OFF", "message": "The 021 market socket is not running in this gateway."})
    return {"requested": len(body.instrument_ids[:200]), "accepted": _md.request(body.instrument_ids[:200])}


@app.get("/v1/market/quote/{instrument_id}")
def quote(instrument_id: str, c: Caller = READ) -> dict:
    """Latest 021 price for one instrument plus its 021 tradability facts (token, tick, lot, circuits)."""
    from countersign.core import market

    with session_scope() as s:
        inst = s.get(Instrument, instrument_id)
        if inst is None or inst.token_021 is None:
            raise HTTPException(404, {"code": "NOT_ON_021", "message": f"{instrument_id} is not mapped to a 021 instrument."})
        facts = {"instrument_id": inst.instrument_id, "symbol": inst.symbol, "token_021": inst.token_021, "exchange_021": inst.exchange_021, "tick_size_paise": inst.tick_size_paise, "lot_size": inst.lot_size, "lower_circuit_paise": inst.lower_circuit_paise, "upper_circuit_paise": inst.upper_circuit_paise}
    t = market.latest(instrument_id)
    if t is None and _md is not None:
        _md.request([instrument_id])
        end = time.time() + 4.0
        while t is None and time.time() < end:
            time.sleep(0.25)
            t = market.latest(instrument_id)
    return {**facts, "tick": t.to_api() if t else None}


@app.get("/v1/market/option-chain")
def option_chain(underlying: str = "NIFTY", expiry: str | None = None, width: int = 8, c: Caller = READ) -> dict:
    return build_option_chain(underlying.upper(), expiry, max(2, min(width, 15)))


def build_option_chain(underlying: str, expiry: str | None, width: int) -> dict:
    from countersign.core import instruments as master
    from countersign.core import market

    spot_t = market.latest(f"NSE:IDX:{underlying}")
    if spot_t is None:
        raise GatewayError(503, "NO_SPOT", f"No live {underlying} index price from 021 yet.")
    spot = spot_t.ltp_paise
    now = int(time.time())
    with session_scope() as s:
        exps = sorted(set(s.execute(select(Instrument021.expiry).where(Instrument021.exchange == "NSEFO", Instrument021.symbol == underlying, Instrument021.instrument_type == "OPTIDX", Instrument021.expiry >= now)).scalars()))
        if not exps:
            raise GatewayError(404, "NO_EXPIRIES", f"No {underlying} option expiries in today's 021 instrument file.")
        dates = [master.expiry_date_ist(e) for e in exps]
        pick = exps[dates.index(expiry)] if expiry in dates else exps[0]
        rows = s.execute(select(Instrument021).where(Instrument021.exchange == "NSEFO", Instrument021.symbol == underlying, Instrument021.instrument_type == "OPTIDX", Instrument021.expiry == pick)).scalars().all()
        strikes = sorted({r.strike for r in rows})
        atm = min(strikes, key=lambda k: abs(k - spot))
        i = strikes.index(atm)
        chosen = strikes[max(0, i - width) : i + width + 1]
        legs: dict[tuple[int, str], dict] = {}
        for r in rows:
            if r.strike not in chosen:
                continue
            fresh = master.option_from_021(r, underlying)
            inst = s.get(Instrument, fresh.instrument_id)
            if inst is None:
                s.add(fresh)
                inst = fresh
            elif inst.token_021 != r.token:
                master.apply_021_row(inst, r)
                inst.source = "021"
            legs[(r.strike, fresh.option_type)] = {"instrument_id": fresh.instrument_id, "token_021": r.token, "lot": r.lot}
        lot = rows[0].lot if rows else master.NIFTY_LOT
    gw().lookup.refresh()
    ids = [v["instrument_id"] for v in legs.values()]
    if _md is not None:
        _md.request(ids)
        end = time.time() + 5.0
        while time.time() < end and len(market.latest_many(ids)) < len(ids):
            time.sleep(0.25)
    ticks = market.latest_many(ids)
    out_rows = []
    step = (chosen[1] - chosen[0]) if len(chosen) > 1 else 5000
    for k in chosen:
        row = {"strike_paise": k, "is_atm": k == atm, "distance": (k - atm) // step if step else 0}
        for opt, name in (("CE", "ce"), ("PE", "pe")):
            leg = legs.get((k, opt))
            t = ticks.get(leg["instrument_id"]) if leg else None
            last = _md.latest(leg["instrument_id"]) if (_md is not None and leg) else None
            row[name] = {
                "instrument_id": leg["instrument_id"] if leg else None,
                "token_021": leg["token_021"] if leg else None,
                "ltp_paise": t.ltp_paise if t else None,
                "oi": int((last or {}).get("oi") or 0),
                "volume": t.volume if t else 0,
                "iv_bps": None,
                "change_bps": int((t.ltp_paise - t.prev_close_paise) * 10000 / t.prev_close_paise) if t and t.prev_close_paise else 0,
                "stale": t.stale if t else True,
            }
        out_rows.append(row)
    return {
        "underlying": underlying,
        "spot_paise": spot,
        "expiry": dates[exps.index(pick)],
        "expiries": dates[:6],
        "atm_strike_paise": atm,
        "strike_step_paise": step,
        "lot_size": lot,
        "rows": out_rows,
        "as_of": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": "021",
        "note": "Live 021 prices. IV is not provided by the 021 full packet, so iv_bps is null.",
    }


# ------------------------------------------------------------------ admin / audit


@app.get("/admin/orders")
def all_orders(c: Caller = Depends(need("admin", "audit"))) -> list[dict]:
    return gw().orders(status="all")


@app.get("/admin/market/trace")
def market_trace(instrument_id: str, c: Caller = Depends(need("admin", "audit"))) -> dict:
    """Last raw 021 updates for one instrument: (time, tc, ltp, bid, ask, close). Diagnostics only."""
    if _md is None:
        raise HTTPException(503, {"code": "MARKET_DATA_OFF", "message": "The 021 market socket is not running in this gateway."})
    return {"instrument_id": instrument_id, "median_window": _md.median, "updates": _md.trace.get(instrument_id, []), "raw_frames": _md.raw_frames[-40:]}


class FaultBody(BaseModel):
    mode: str
    count: int = 1


@app.get("/admin/faults")
def get_faults(c: Caller = Depends(need("admin"))) -> dict:
    return {"enabled": gateway_settings().test_faults, **_faults}


@app.post("/admin/faults")
def set_faults(body: FaultBody, c: Caller = Depends(need("admin"))) -> dict:
    """Drill only (CS_021_TEST_FAULTS=true): drop the HTTP answer of the next placement AFTER 021 accepted it, to show
    that reconciliation finds the order and nothing is sent twice. Off by default; never affects 021 itself."""
    if not gateway_settings().test_faults:
        raise HTTPException(403, {"code": "FAULTS_DISABLED", "message": "Fault drills are disabled on this gateway (CS_021_TEST_FAULTS is not true)."})
    if body.mode not in ("none", "drop_after_accept"):
        raise HTTPException(422, {"code": "UNKNOWN_FAULT", "message": "Fault must be 'none' or 'drop_after_accept'."})
    _faults["drop_after_accept"] = 0 if body.mode == "none" else max(1, min(body.count, 3))
    return {"enabled": True, **_faults}


def main() -> None:
    import uvicorn

    uvicorn.run("countersign.broker021.app:app", host="127.0.0.1", port=gateway_settings().port, log_level="warning")


if __name__ == "__main__":
    main()
