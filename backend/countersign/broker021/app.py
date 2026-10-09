"""gateway021: the ONLY process that holds 021 passwords and access tokens.

It exposes the paper broker's /v1 interface and key scopes (read, write, admin, audit; the naive key is refused), so
ReadGateway, OrderExecutionProvider and BrokerAdmin reach the real 021 sandbox by base URL alone. It serves several 021
accounts: the one configured in .env (CS_021_UCC / CS_021_PASSWORD) and every account a trader linked through
self-service linking (POST /admin/link; the password arrives encrypted to this process's RSA key and is stored sealed
with its AES-GCM key, see vault.py). Each account has its own Gateway (one token, single-flight login, write-ahead
attempts, reconciliation, no automatic retry); X-Account picks it, and an unknown or unlinked account is refused.
One market-data websocket runs (the .env account's, else the first linked account's); order updates for linked
accounts come from REST polling of their order book every 3 s (the orders websocket runs for the primary account).

Run: python -m countersign.broker021.app   (port 8200; started by scripts/dev.py when CS_021_UCC is configured)"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, SecretStr
from sqlalchemy import select

from countersign.broker021 import instruments021, store
from countersign.broker021.client import Client021
from countersign.broker021.gateway import Gateway, GatewayError
from countersign.broker021.settings import gateway_settings
from countersign.broker021.vault import VaultError, vault
from countersign.core import events
from countersign.core.config import settings
from countersign.core.db import session_scope
from countersign.core.models import Instrument, Instrument021

STATUS_KEY = "cs:gw021:status"

app = FastAPI(title="Countersign 021 gateway")
_gw: Gateway | None = None  # the .env account (or an unconfigured placeholder whose calls fail closed)
_linked: dict[str, Gateway] = {}  # self-service linked accounts by UCC (the .env UCC is never in here)
_transport = None  # tests inject an httpx MockTransport for linked accounts' clients
_lock = threading.RLock()  # guards _linked, the market socket start and linking
_md = None
_ows = None
_boot: dict = {"instruments": None, "recovered": [], "started_at": None, "error": None}
_faults = {"drop_after_accept": 0, "fail_before_send": 0}
_used_nonces: dict[str, float] = {}  # link nonces already opened here (defence in depth; the API consumes them first)
UCC_RE = re.compile(r"^[A-Z0-9]{4,16}$")


def _publish():
    return (lambda order: None) if gateway_settings().isolated else None


def _env_gw() -> Gateway | None:
    """The account configured in .env, or None."""
    global _gw
    if _gw is None and gateway_settings().configured:
        _gw = Gateway(gateway_settings(), publish=_publish())
    return _gw if (_gw is not None and _gw.account_id) else None


def gw() -> Gateway:
    """The primary account: the .env account, else the first linked account. Instruments, the market socket and the
    status heartbeat use it."""
    global _gw
    g = _env_gw() or next(iter(_linked.values()), None)
    if g is None:
        if _gw is None:
            _gw = Gateway(gateway_settings(), publish=_publish())
        return _gw
    return g


def gateways() -> list[Gateway]:
    env = _env_gw()
    return ([env] if env else []) + list(_linked.values())


def account_gw(ucc: str) -> Gateway | None:
    env = _env_gw()
    if env is not None and ucc and ucc == env.account_id:
        return env
    return _linked.get(ucc)


def _stored_password(ucc: str) -> str:
    hit = store.latest_credential(ucc)
    if hit is None:
        return ""
    try:
        return vault().unseal(hit[1], ucc, hit[0])
    except VaultError:
        return ""


def _linked_client(ucc: str) -> Client021:
    cfg = gateway_settings().model_copy(update={"ucc": ucc, "password": SecretStr("")})
    return Client021(cfg, transport=_transport, password=lambda: _stored_password(ucc), stop_on_rejected=True)


def _add_linked(ucc: str, client: Client021) -> Gateway:
    lookup = _gw.lookup if _gw is not None else None
    g = Gateway(client.cfg, client=client, lookup=lookup, publish=_publish())
    _linked[ucc] = g
    return g


def account_key(ucc: str) -> str:
    """A stable, non-reversible key for one account in the status heartbeat (which never carries a full UCC)."""
    return hashlib.sha256(ucc.encode()).hexdigest()[:16]


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
    if scope in ("read", "write") and x_account and account_gw(x_account) is None:
        raise HTTPException(403, {"code": "ACCOUNT_FORBIDDEN", "message": "This gateway does not serve that 021 account (it is not configured or linked)."})
    return Caller(scope=scope)


def need(*scopes: str):
    def dep(c: Caller = Depends(caller)) -> Caller:
        if c.scope not in scopes:
            raise HTTPException(403, {"code": "SCOPE_FORBIDDEN", "message": f"Key scope '{c.scope}' cannot perform this operation."})
        return c

    return dep


def acct(x_account: str = Header(default="")) -> Gateway:
    """The session of the account named in X-Account; an unknown or unlinked account is refused (fail closed)."""
    if not x_account:
        raise HTTPException(400, {"code": "ACCOUNT_REQUIRED", "message": "Send X-Account with the 021 account id."})
    g = account_gw(x_account)
    if g is None:
        raise HTTPException(403, {"code": "ACCOUNT_FORBIDDEN", "message": "This gateway does not serve that 021 account (it is not configured or linked)."})
    return g


def _local_only(request: Request) -> None:
    peer = request.client.host if request.client else ""
    if peer not in ("127.0.0.1", "::1"):
        raise HTTPException(403, {"code": "LOCAL_ONLY", "message": "Account linking is accepted from this machine only."})


READ = Depends(need("read", "write", "audit"))


@app.exception_handler(HTTPException)
def _http_exc(_: Request, exc: HTTPException):
    d = exc.detail if isinstance(exc.detail, dict) else {"code": "ERROR", "message": str(exc.detail)}
    return JSONResponse(d, status_code=exc.status_code)


@app.exception_handler(GatewayError)
def _gw_exc(_: Request, exc: GatewayError):
    return JSONResponse(exc.body(), status_code=exc.status)


# ------------------------------------------------------------------ lifecycle


def _account_status(g: Gateway, source: str) -> dict:
    c = g.client
    return {
        "account": mask(g.account_id),
        "source": source,
        "logged_in": c.logged_in,
        "token_expires_at": datetime.fromtimestamp(c.token_expires_at, UTC).isoformat().replace("+00:00", "Z") if c.token_expires_at else None,
        "credentials_rejected": c.credentials_rejected,
        "last_error": c.stats.last_error,
        "poll_error": _boot.get("poll_errors", {}).get(g.account_id),
    }


def _status() -> dict:
    g = gw()
    st = g.client.stats
    env = _env_gw()
    return {
        "ok": g.client.logged_in or _boot["error"] is None,
        "broker": "021",
        "account": mask(g.account_id),
        "accounts": {account_key(x.account_id): _account_status(x, "env" if x is env else "linked") for x in gateways()},
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


def _poller(g: Gateway) -> None:
    """REST polling of one account's order book every 3 s; stops when the account is unlinked."""
    first = True
    errors = _boot.setdefault("poll_errors", {})
    while g is account_gw(g.account_id):
        try:
            g.sync(publish_all=first)
            first = False
            errors.pop(g.account_id, None)
        except Exception as e:  # noqa: BLE001
            _boot["poll_error"] = errors[g.account_id] = f"{type(e).__name__}: {str(e)[:200]}"
        time.sleep(3.0)


def _start_poller(g: Gateway) -> None:
    threading.Thread(target=_poller, args=(g,), daemon=True, name=f"g021-poller-{account_key(g.account_id)[:6]}").start()


def _ensure_market() -> None:
    """One market-data websocket for the whole gateway, on the primary account (started when the first account
    becomes available if none was at startup)."""
    global _md
    cfg = gateway_settings()
    with _lock:
        if _md is not None or not cfg.market_ws or not gw().account_id:
            return
        from countersign.broker021.marketdata import MarketData

        _md = MarketData(gw(), cfg.ws_base, publish_ticks=cfg.publish_ticks, isolated=cfg.isolated, median=cfg.tick_median)
        _md.books = gateways
        threading.Thread(target=_md.run_forever, daemon=True, name="g021-market").start()


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


def _load_linked() -> None:
    """Every self-service linked account gets its own session (logged in lazily with its unsealed password)."""
    env = _env_gw()
    for ucc in store.linked_uccs():
        if (env is not None and ucc == env.account_id) or ucc in _linked:
            continue
        _add_linked(ucc, _linked_client(ucc))


@app.on_event("startup")
def _startup() -> None:
    global _ows
    cfg = gateway_settings()
    _boot["started_at"] = time.time()
    from countersign.core.models import create_all

    create_all()
    store.init_db()
    vault().ensure_keys()  # creates the link key pair and the store key once, under .secrets/
    with _lock:
        _load_linked()
    if not cfg.configured and not _linked:
        _boot["error"] = "No 021 account yet: CS_021_UCC / CS_021_PASSWORD are not configured and no trader has linked one."
        threading.Thread(target=_heartbeat, daemon=True, name="g021-heartbeat").start()
        threading.Thread(target=_daily_instruments, daemon=True, name="g021-instruments").start()  # loads once one is linked
        return
    try:
        _boot["instruments"] = instruments021.ensure_loaded(gw().client, cfg.instruments_cache)
        gw().lookup.refresh()
    except Exception as e:  # noqa: BLE001
        _boot["error"] = f"instrument load failed: {type(e).__name__}: {str(e)[:200]}"
    _boot["recovered"] = []
    for g in gateways():
        try:
            _boot["recovered"] += g.recover()
        except Exception as e:  # noqa: BLE001
            _boot["error"] = f"startup reconciliation failed for {mask(g.account_id)}: {type(e).__name__}: {str(e)[:200]}"
        g.client.drop_next_write_responses = 0
        _start_poller(g)
    threading.Thread(target=_heartbeat, daemon=True, name="g021-heartbeat").start()
    threading.Thread(target=_daily_instruments, daemon=True, name="g021-instruments").start()
    _ensure_market()
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
    """The account configured in .env (the operator-linked account), or null when only linked accounts exist."""
    env = _env_gw()
    return {"account_id": env.account_id if env else None, "broker": "021", "label": mask(env.account_id) if env else None}


@app.get("/v1/funds")
def funds(c: Caller = READ, g: Gateway = Depends(acct)) -> dict:
    return g.funds()


@app.get("/v1/holdings")
def holdings(c: Caller = READ, g: Gateway = Depends(acct)) -> list[dict]:
    return g.holdings()


@app.get("/v1/positions")
def positions(c: Caller = READ, g: Gateway = Depends(acct)) -> list[dict]:
    return g.positions()


@app.get("/v1/orders")
def orders(status: str = "all", tag: str | None = None, c: Caller = READ, g: Gateway = Depends(acct)) -> list[dict]:
    return g.orders(status=status, tag=tag)


@app.get("/v1/orders/{oid}")
def order(oid: str, c: Caller = READ, g: Gateway = Depends(acct)) -> dict:
    return g.order(oid)


@app.get("/v1/trades")
def trades(c: Caller = READ, g: Gateway = Depends(acct)) -> list[dict]:
    return g.trades()


@app.get("/v1/attempts/{tag}")
def attempt(tag: str, wait_s: float = 0.0, c: Caller = READ, g: Gateway = Depends(acct)) -> dict:
    a = g.attempt(tag, wait_s=max(0.0, min(wait_s, 25.0)))
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


@app.post("/v1/orders")
def place(body: PlaceBody, c: Caller = Depends(need("write")), g: Gateway = Depends(acct)) -> dict:
    if _faults["fail_before_send"] > 0:
        _faults["fail_before_send"] -= 1
        g.client.fail_next_write_connects = 1
    elif _faults["drop_after_accept"] > 0:
        _faults["drop_after_accept"] -= 1
        g.client.drop_next_write_responses = 1
    try:
        return g.place(body.model_dump())
    finally:
        g.client.drop_next_write_responses = 0
        g.client.fail_next_write_connects = 0


class ModifyBody(BaseModel):
    quantity: int | None = None
    limit_price_paise: int | None = None
    trigger_price_paise: int | None = None


@app.put("/v1/orders/{oid}")
def modify(oid: str, body: ModifyBody, c: Caller = Depends(need("write")), g: Gateway = Depends(acct)) -> dict:
    return g.modify(oid, body.quantity, body.limit_price_paise, body.trigger_price_paise)


@app.delete("/v1/orders/{oid}")
def cancel(oid: str, c: Caller = Depends(need("write")), g: Gateway = Depends(acct)) -> dict:
    return g.cancel(oid)


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
    """Every managed account's order book (each order carries its account_id) for the independent monitor. The
    primary account failing fails the whole read (fail closed, as before); a linked account that cannot be read
    (for example its stored password was refused) is skipped and reported in the gateway status."""
    out: list[dict] = []
    primary = gw()
    out += primary.orders(status="all")
    errors = _boot.setdefault("poll_errors", {})
    for g in gateways():
        if g is primary:
            continue
        try:
            out += g.orders(status="all")
        except GatewayError as e:
            errors[g.account_id] = f"order book unreadable: {e.code}"
    return out


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
    """Drill only (CS_021_TEST_FAULTS=true), for the next 1 to 3 placements: 'drop_after_accept' drops the HTTP answer
    AFTER 021 accepted the order (reconciliation finds it, nothing is sent twice); 'fail_before_send' fails the
    connection before any byte is sent (the order is NOT_PLACED and the trader countersigns again to resend). Off by
    default; never changes what 021 itself does."""
    if not gateway_settings().test_faults:
        raise HTTPException(403, {"code": "FAULTS_DISABLED", "message": "Fault drills are disabled on this gateway (CS_021_TEST_FAULTS is not true)."})
    if body.mode not in ("none", "drop_after_accept", "fail_before_send"):
        raise HTTPException(422, {"code": "UNKNOWN_FAULT", "message": "Fault must be 'none', 'drop_after_accept' or 'fail_before_send'."})
    n = max(1, min(body.count, 3))
    _faults["drop_after_accept"] = n if body.mode == "drop_after_accept" else 0
    _faults["fail_before_send"] = n if body.mode == "fail_before_send" else 0
    return {"enabled": True, **_faults}


# ------------------------------------------------------------------ self-service linking (admin key, this machine only)


@app.get("/v1/link-key")
def link_key(c: Caller = Depends(need("read", "admin"))) -> dict:
    """The public half of the link key pair (SPKI, base64). The browser encrypts the trader's UCC and password to it."""
    return {"public_key": vault().public_key_spki_b64(), "alg": "RSA-OAEP-256", "max_plaintext_bytes": 190}


class LinkBody(BaseModel):
    ciphertext: str
    nonce: str
    user_id: str


def _fresh_nonce(nonce: str) -> bool:
    now = time.time()
    for k, at in list(_used_nonces.items()):
        if now - at > 900:
            _used_nonces.pop(k, None)
    if nonce in _used_nonces:
        return False
    _used_nonces[nonce] = now
    return True


def _refuse(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "message": message})


@app.post("/admin/link")
def link(body: LinkBody, request: Request, c: Caller = Depends(need("admin"))) -> dict:
    """Opens the browser's ciphertext, checks it carries the nonce and user id the API expected, validates the UCC and
    verifies the password with exactly one 021 login. Only a verified password is stored (sealed). Returns the account
    id (the UCC, which is the 021 account id everywhere in Countersign), its mask and the session status; never the
    password, the token or the ciphertext."""
    _local_only(request)
    if len(body.ciphertext) > 2048 or not body.nonce or not body.user_id:
        _refuse(400, "LINK_INVALID", "The link request is malformed.")
    try:
        data = vault().open_link(body.ciphertext)
    except VaultError:
        _refuse(400, "LINK_DECRYPT_FAILED", "The encrypted credentials could not be opened. Reload the page and try again.")
    if not (hmac.compare_digest(str(data.get("nonce") or ""), body.nonce) and hmac.compare_digest(str(data.get("user_id") or ""), body.user_id)):
        _refuse(400, "LINK_NONCE_MISMATCH", "These encrypted credentials were not made for this request. Reload the page and try again.")
    if not _fresh_nonce(body.nonce):
        _refuse(400, "LINK_REPLAYED", "These encrypted credentials were already used. Reload the page and try again.")
    ucc = str(data.get("ucc") or "").strip().upper()
    password = data.get("password")
    del data
    if not UCC_RE.match(ucc):
        _refuse(422, "UCC_INVALID", "The UCC (021 client code) must be 4 to 16 letters or digits.")
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        _refuse(422, "PASSWORD_INVALID", "Enter your 021 password.")
    with _lock:
        existing = account_gw(ucc)
        client = existing.client if existing is not None else _linked_client(ucc)
        # One login with the entered password. For an account this gateway already serves, the same client takes the
        # new token, so there is never a second session of the same account in this process.
        kind, detail = client.verify_login(password)
        if kind != "ok":
            if existing is None:
                client.close()
            if kind == "rejected":
                _refuse(401, "LINK_WRONG_CREDENTIALS", "021 did not accept that UCC and password. Check both and try again. Nothing was stored.")
            if kind == "refused":
                _refuse(422, "LINK_REFUSED", f"021 refused the login ({detail}). Nothing was stored.")
            _refuse(503, "LINK_021_UNAVAILABLE", "021 did not answer, try again. Nothing was stored.")
        store.save_credential(ucc, body.user_id, vault().seal(password, ucc, body.user_id))
        del password
        env = _env_gw()
        g = existing
        if g is None:
            g = _add_linked(ucc, client)
            _start_poller(g)
        elif g is not env:
            g.client.credentials_rejected = False
    _ensure_market()
    return {"account_id": ucc, "masked": mask(ucc), "status": "linked", "source": "env" if g is env else "linked", "logged_in": g.client.logged_in, "token_expires_at": _account_status(g, "")["token_expires_at"]}


class UnlinkBody(BaseModel):
    account_id: str
    user_id: str


@app.post("/admin/unlink")
def unlink(body: UnlinkBody, request: Request, c: Caller = Depends(need("admin"))) -> dict:
    """Deletes the trader's stored credential for this account. When no linked trader is left, the account's session is
    dropped (its token forgotten, polling stops). The .env account always keeps its session."""
    _local_only(request)
    ucc = body.account_id.strip().upper()
    with _lock:
        deleted = store.delete_credential(ucc, body.user_id)
        env = _env_gw()
        dropped = False
        if ucc in _linked and not store.has_credential(ucc):
            g = _linked.pop(ucc)
            g.client.drop_token()
            dropped = True
    return {"account": mask(ucc), "deleted": deleted, "session_dropped": dropped, "env_account": env is not None and env.account_id == ucc}


def main() -> None:
    import uvicorn

    uvicorn.run("countersign.broker021.app:app", host="127.0.0.1", port=gateway_settings().port, log_level="warning")


if __name__ == "__main__":
    main()
