"""Gateway core: the paper broker's /v1 semantics on top of the 021 REST API, for exactly one 021 account.

Placement (never retried):
  1. Map the signed order to 021 exactly (mapping.to_021_order) or refuse it.
  2. Under the placement lock: refuse a tag that was already used (a tag is never sent twice) and refuse a second
     placement with the same fingerprint while one is unresolved (409 DUPLICATE_IN_FLIGHT). Write the attempt row
     (write-ahead), then send POST /orders once.
  3. Success with an orderId: map tag -> orderId. Connection never established: NOT_PLACED. Anything else (timeout,
     dropped connection, 429, 500, 503, 401, even a definite 4xx): reconcile against GET /orders before answering:
     orders placed at or after (send time - skew, on 021's clock) with the same fingerprint and not mapped to any
     other tag. Exactly one: mapped, reported with its real 021 status. None: NOT_PLACED with the original error (or
     REJECTED when 021 refused it). More than one: AMBIGUOUS, never guessed. Order book unreadable: UNCONFIRMED."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from countersign.broker021 import mapping, store
from countersign.broker021.client import Api021Error, Api021Unavailable, Client021, LoginFailed
from countersign.broker021.instruments021 import Lookup
from countersign.broker021.settings import GatewaySettings
from countersign.core import events
from countersign.core.config import settings

OPEN_STATUSES = ("OPEN", "TRIGGER_PENDING", "PARTIALLY_FILLED")


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _publish_order(order: dict) -> None:
    events.publish(events.CH_BROKER_UPDATES, {"order": order})


class Gateway:
    def __init__(self, cfg: GatewaySettings, client: Client021 | None = None, lookup: Lookup | None = None, publish: Callable[[dict], None] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.client = client or Client021(cfg)
        self.lookup = lookup or Lookup()
        self.account_id = cfg.ucc
        self.publish = publish or _publish_order
        self._sleep = sleep
        self._place_lock = threading.Lock()
        self._book_lock = threading.Lock()
        self._book: dict[str, dict] = {}  # orderId -> latest raw 021 row
        self._book_at = 0.0
        self._seen: dict[str, tuple] = {}  # orderId -> last published state key
        self._fills: dict[str, tuple[int, int | None, str | None]] = {}  # orderId -> (traded qty, avg fill, side from fills)
        self._cache: dict[str, tuple[float, Any]] = {}
        self.fingerprint_hold_s = 30.0
        self.last_reconciliation: dict | None = None

    # ------------------------------------------------------------------ small helpers

    def _cached(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (time.time(), val)
        return val

    def invalidate(self) -> None:
        self._cache.clear()

    def _read(self, path: str) -> Any:
        try:
            return self.client.get(path)
        except Api021Error as e:
            if e.status == 404:
                raise GatewayError(404, "NOT_FOUND", e.error) from None
            if e.status == 401:
                raise GatewayError(503, "BROKER_AUTH_FAILED", "021 refused the access token twice; reads are failing closed.") from None
            raise GatewayError(502, "BROKER_ERROR", f"021 answered HTTP {e.status}: {e.error}") from None
        except Api021Unavailable as e:
            raise GatewayError(503, "BROKER_UNAVAILABLE", str(e)) from None
        except LoginFailed as e:
            raise GatewayError(503, "BROKER_AUTH_FAILED", str(e)) from None

    def _fill_info(self, oid: str, traded: int) -> tuple[int | None, str | None]:
        """(average fill price, side from the signed fill quantities) from GET /orders/{id}/trades, cached per fill."""
        if traded == 0:
            return None, None
        hit = self._fills.get(oid)
        if hit and hit[0] == traded:
            return hit[1], hit[2]
        try:
            trades = self.client.get(f"/orders/{oid}/trades")
        except (Api021Error, Api021Unavailable, LoginFailed):
            return (hit[1], hit[2]) if hit else (None, None)
        trades = trades if isinstance(trades, list) else []
        signed = sum(int(x.get("quantity") or 0) for x in trades)
        side = "BUY" if signed > 0 else "SELL" if signed < 0 else None
        avg = mapping.avg_price(trades)
        self._fills[oid] = (traded, avg, side)
        return avg, side

    def to_order(self, row: dict, attempt: dict | None = None) -> dict:
        inst = self.lookup.instrument_for(row.get("exchange") or "", int(row.get("token") or 0))
        oid = str(row.get("orderId"))
        traded = abs(int(row.get("qtyTraded") or 0))
        avg, side = self._fill_info(oid, traded)
        return mapping.order_json(row, account_id=self.account_id, instrument=inst, attempt=attempt, avg_fill=avg, side_hint=side)

    # ------------------------------------------------------------------ order book

    def raw_orders(self, max_age: float = 0.0) -> list[dict]:
        with self._book_lock:
            if max_age and time.time() - self._book_at < max_age:
                return list(self._book.values())
        rows = self._read("/orders")
        rows = rows if isinstance(rows, list) else []
        with self._book_lock:
            self._book = {str(o.get("orderId")): o for o in rows}
            self._book_at = time.time()
        return rows

    def raw_order(self, oid: str) -> dict:
        rows = self._read(f"/orders/{oid}")
        if not rows:
            raise GatewayError(404, "ORDER_NOT_FOUND", f"No order {oid}.")
        row = rows[0] if isinstance(rows, list) else rows
        with self._book_lock:
            self._book[str(row.get("orderId"))] = row
        return row

    def sync(self, publish_all: bool = False) -> int:
        """One poll of today's order book: publishes every order whose state changed (to the tracker), and maps a
        placement that 021 shows only after it was reported NOT_PLACED (late acceptance). Returns orders changed."""
        rows = self.raw_orders()
        tags = store.tags_by_order(self.account_id)
        self._late_match(rows, tags)
        tags = store.tags_by_order(self.account_id)
        changed = 0
        for row in rows:
            oid = str(row.get("orderId"))
            key = (row.get("status"), row.get("qtyTraded"), row.get("qtyRemaining"), row.get("price"), row.get("triggerPrice"))
            if not publish_all and self._seen.get(oid) == key:
                continue
            self._seen[oid] = key
            self.publish(self.to_order(row, tags.get(oid)))
            changed += 1
        return changed

    def refresh_one(self, oid: str) -> dict | None:
        """Re-reads one order from REST (the source of truth) after an orders-socket event and publishes it."""
        try:
            row = self.raw_order(oid)
        except GatewayError:
            return None
        key = (row.get("status"), row.get("qtyTraded"), row.get("qtyRemaining"), row.get("price"), row.get("triggerPrice"))
        order = self.to_order(row, store.by_order_id(oid))
        if self._seen.get(oid) != key:
            self._seen[oid] = key
            self.publish(order)
        return order

    def _late_match(self, rows: list[dict], tags: dict[str, dict]) -> None:
        late = store.late_candidates(self.account_id, self.cfg.late_match_window_s)
        if not late:
            return
        unmapped = [r for r in rows if str(r.get("orderId")) not in tags]
        for att in late:
            fp = self._fp_fields(att["order_021"])
            hits = [r for r in unmapped if mapping.matches(r, fp, self._not_before(att))]
            if len(hits) == 1:
                oid = str(hits[0].get("orderId"))
                twins = [a for a in late if a is not att and mapping.matches(hits[0], self._fp_fields(a["order_021"]), self._not_before(a))]
                if twins:
                    continue  # two unresolved placements could own it: never guess
                store.update(att["tag"], state="REJECTED" if hits[0].get("status") == "Rejected" else "ACCEPTED", order_id=oid, note=f"Late: 021 showed this order after it was reported {att['state']}. It was not resent.")
                self._seen.pop(oid, None)  # republish it, now carrying its tag
                unmapped = [r for r in unmapped if str(r.get("orderId")) != oid]

    # ------------------------------------------------------------------ reads in the paper broker's shape

    def order(self, oid: str) -> dict:
        row = self.raw_order(oid)
        return self.to_order(row, store.by_order_id(oid))

    def orders(self, status: str = "all", tag: str | None = None) -> list[dict]:
        if tag:
            att = self._wait_resolved(tag, 20.0)
            if att is None or not att.get("broker_order_id"):
                return []
            oid = att["broker_order_id"]
            with self._book_lock:
                row = self._book.get(oid) if time.time() - self._book_at < 2.0 else None
            if row is None:
                row = self.raw_order(oid)
            out = [self.to_order(row, att)]
        else:
            rows = self._cached("orders", 2.0, lambda: self.raw_orders())
            tags = store.tags_by_order(self.account_id)
            out = [self.to_order(r, tags.get(str(r.get("orderId")))) for r in rows]
        if status == "open":
            out = [o for o in out if o["status"] in OPEN_STATUSES]
        elif status == "rejected":
            out = [o for o in out if o["status"] == "REJECTED"]
        return out

    def _wait_resolved(self, tag: str, timeout_s: float) -> dict | None:
        end = time.time() + timeout_s
        att = store.get(tag)
        while att is not None and att["state"] == "SENDING" and time.time() < end:
            self._sleep(0.3)
            att = store.get(tag)
        return att

    def attempt(self, tag: str, wait_s: float = 0.0) -> dict | None:
        att = self._wait_resolved(tag, wait_s) if wait_s else store.get(tag)
        if att is None:
            return None
        return {k: att[k] for k in ("tag", "state", "broker_order_id", "http_status", "error", "candidates", "note", "sent_at", "resolved_at")}

    def trades(self) -> list[dict]:
        rows = self._cached("trades", 1.0, lambda: self._read("/trades"))
        out = []
        for t in rows if isinstance(rows, list) else []:
            inst = self.lookup.instrument_for(t.get("exchange") or "", int(t.get("token") or 0))
            out.append({"trade_id": str(t.get("tradeId")), "broker_order_id": None, "instrument_id": inst["instrument_id"], "symbol": inst["symbol"], "quantity": int(t.get("quantity") or 0), "price_paise": int(t.get("price") or 0), "product": mapping.PRODUCT_FROM_021.get(t.get("product") or "", t.get("product")), "ts": mapping._iso(t.get("tradeTime"))})
        return out

    def positions(self) -> list[dict]:
        rows = self._cached("positions", 1.0, lambda: self._read("/portfolio/positions"))
        trades = self._cached("trades", 1.0, lambda: self._read("/trades"))
        realised = mapping.realised_by_position(trades if isinstance(trades, list) else [])
        out = []
        for p in rows if isinstance(rows, list) else []:
            if (p.get("product") or "") == "CNC" and mapping.SEGMENT_OF.get(p.get("exchange") or "") == "EQ":
                continue  # delivery (CNC) trades of today are reported under holdings, like the paper broker
            inst = self.lookup.instrument_for(p.get("exchange") or "", int(p.get("token") or 0))
            q = int(p.get("netQuantity") or 0)
            r = realised.get((p.get("exchange") or "", int(p.get("token") or 0), p.get("product") or ""), 0)
            if q == 0 and r == 0:
                continue
            out.append(
                {
                    "instrument_id": inst["instrument_id"],
                    "symbol": inst["symbol"],
                    "product": mapping.PRODUCT_FROM_021.get(p.get("product") or "", p.get("product")),
                    "quantity": q,
                    "avg_price_paise": int(p.get("netPrice") or 0),
                    "realised_paise": r,
                    "prev_close_paise": int(p.get("prevClose") or 0),
                    "buy_price_paise": int(p.get("buyPrice") or 0),
                    "sell_price_paise": int(p.get("sellPrice") or 0),
                    "square_off_quantity": int(p.get("squareOffQuantity") or 0),
                    "carry_forward_quantity": int(p.get("carryForwardQuantity") or 0),
                    "exchange_021": p.get("exchange"),
                    "token_021": p.get("token"),
                }
            )
        return out

    def holdings(self) -> list[dict]:
        """Delivery holdings in the paper broker's sense: settled and T+1 shares from 021's holdings plus today's net
        delivery (CNC) trades from 021's positions, so a stock bought today can be sold today and nothing is counted
        twice (positions() leaves CNC rows out)."""
        rows = self._cached("holdings", 1.0, lambda: self._read("/portfolio/holdings"))
        pos = self._cached("positions", 1.0, lambda: self._read("/portfolio/positions"))
        merged: dict[str, dict] = {}
        for h in rows if isinstance(rows, list) else []:
            inst = self.lookup.instrument_for_isin(h.get("isin") or "", h.get("symbol")) or {"instrument_id": f"021:ISIN:{h.get('isin')}", "symbol": h.get("symbol") or h.get("isin")}
            q = int(h.get("freeQty") or 0) + int(h.get("btstQty") or 0)
            merged[inst["instrument_id"]] = {
                "instrument_id": inst["instrument_id"],
                "symbol": inst["symbol"],
                "quantity": q,
                "avg_price_paise": int(h.get("price") or 0),
                "prev_close_paise": int(h.get("prevClose") or 0),
                "isin": h.get("isin"),
                "free_quantity": int(h.get("freeQty") or 0),
                "btst_quantity": int(h.get("btstQty") or 0),
                "sell_quantity": int(h.get("sellQty") or 0),
                "today_quantity": 0,
            }
        for p in pos if isinstance(pos, list) else []:
            if (p.get("product") or "") != "CNC" or mapping.SEGMENT_OF.get(p.get("exchange") or "") != "EQ":
                continue
            net = int(p.get("netQuantity") or 0)
            if net == 0:
                continue
            inst = self.lookup.instrument_for(p.get("exchange") or "", int(p.get("token") or 0))
            h = merged.setdefault(inst["instrument_id"], {"instrument_id": inst["instrument_id"], "symbol": inst["symbol"], "quantity": 0, "avg_price_paise": 0, "prev_close_paise": int(p.get("prevClose") or 0), "isin": None, "free_quantity": 0, "btst_quantity": 0, "sell_quantity": 0, "today_quantity": 0})
            if net > 0:
                held = h["quantity"]
                h["avg_price_paise"] = (h["avg_price_paise"] * held + int(p.get("netPrice") or p.get("buyPrice") or 0) * net) // (held + net)
            h["quantity"] += net
            h["today_quantity"] += net
        return [h for h in merged.values() if h["quantity"] > 0]

    def funds(self) -> dict:
        """021 has no funds or margin API. This is a clearly labelled ESTIMATE: the virtual daily funds minus the value
        of open buy orders and today's net buys. 021's own risk checks decide whether an order is accepted."""
        from countersign.core import market

        daily = settings().funds_021_daily_paise
        orders = self.orders(status="open")
        open_buy = 0
        for o in orders:
            if o["side"] != "BUY":
                continue
            px = o["price_021_paise"] or 0
            if not px:
                t = market.latest(o["instrument_id"])
                px = t.ltp_paise if t else 0
            open_buy += o["pending_quantity"] * px
        trades = self._cached("trades", 1.0, lambda: self._read("/trades"))
        net_buys = sum(int(t.get("quantity") or 0) * int(t.get("price") or 0) for t in (trades if isinstance(trades, list) else []))
        cash = daily - max(0, net_buys)
        return {
            "account_id": (self.account_id[:4] + "***" + self.account_id[-1:]) if len(self.account_id) > 5 else "***",
            "cash_paise": cash,
            "used_margin_paise": open_buy,
            "available_paise": cash - open_buy,
            "estimate": True,
            "basis": "021 has no funds API. Estimate = Rs 10,00,000 virtual daily funds - open buy orders - today's net buys. 021's own risk checks decide.",
        }

    # ------------------------------------------------------------------ placement

    def _fp_fields(self, o21: dict) -> dict:
        return {"exchange": mapping.RESPONSE_EXCHANGE.get(o21["exchange"], o21["exchange"]), "token": o21["token"], "qty": o21["qty"], "price": o21["price"], "book": o21["book"], "trigger": o21.get("trigger") or 0, "product": o21["product"], "validity": o21["validity"]}

    def _not_before(self, att: dict) -> float:
        return att["sent_at"] + self.client.clock_offset_s - self.cfg.clock_skew_s

    def _attempt_answer(self, att: dict) -> dict:
        """A tag that was already used is never sent again; its recorded outcome is returned instead."""
        if att["state"] in ("ACCEPTED", "REJECTED") and att.get("broker_order_id"):
            return self.order(att["broker_order_id"]) | {"replayed_tag": True}
        if att["state"] == "SENDING":
            att = self._wait_resolved(att["tag"], 20.0) or att
            if att["state"] in ("ACCEPTED", "REJECTED") and att.get("broker_order_id"):
                return self.order(att["broker_order_id"]) | {"replayed_tag": True}
        raise GatewayError(409, "TAG_ALREADY_USED", f"Tag {att['tag']} was already used ({att['state']}); a tag is never sent twice.", {"attempt_state": att["state"]})

    def place(self, body: dict) -> dict:
        tag = body.get("tag")
        if not tag:
            raise GatewayError(422, "TAG_REQUIRED", "Every order sent through the gateway needs a client tag.")
        tok = self.lookup.token_for(body.get("instrument_id") or "")
        if tok is None:
            raise GatewayError(422, "INSTRUMENT_NOT_ON_021", f"{body.get('symbol') or body.get('instrument_id')} is not in today's 021 instrument file. Nothing was sent.")
        try:
            o21 = mapping.to_021_order(body, tok[0], tok[1])
        except mapping.MappingError as e:
            raise GatewayError(422, "UNMAPPABLE_ORDER", f"{e} Nothing was sent.") from None
        fp = mapping.request_fingerprint(o21)
        with self._place_lock:
            existing = store.get(tag)
            if existing is not None:
                return self._attempt_answer(existing)
            hold = store.holding_fingerprint(fp, self.account_id, self.fingerprint_hold_s)
            if hold is not None:
                raise GatewayError(409, "DUPLICATE_IN_FLIGHT", f"An identical order (tag {hold['tag']}, {hold['state']}) is still being confirmed with 021, so this one was not sent. Nothing was sent twice.", {"holding_tag": hold["tag"]})
            store.create(tag, self.account_id, fp, dict(body), o21)
            res = self.client.write("POST", "/orders", o21)
            store.update(tag, sent_at=res.sent_at or time.time(), http_status=res.status)
        self.invalidate()
        if res.kind == "ok":
            oid = str((res.data or {}).get("orderId") or "").strip()
            if oid:
                store.update(tag, state="ACCEPTED", order_id=oid)
                return self._accepted_order(tag, oid, o21)
            res.error = "021 answered success without an orderId."
        if res.kind == "not_sent":
            store.update(tag, state="NOT_PLACED", error=res.error)
            raise GatewayError(502, "NOT_PLACED", f"{res.error} Nothing was sent to 021.", {"reconciled": False, "broker_error": res.error})
        return self._reconcile_place(tag, res)

    def _accepted_order(self, tag: str, oid: str, o21: dict) -> dict:
        att = store.get(tag)
        for i in range(3):
            try:
                row = self.raw_order(oid)
                return self.to_order(row, att)
            except GatewayError:
                self._sleep(0.4 * (i + 1))
        # Accepted (021 gave the orderId) but not yet readable: report it as received; the poller publishes the
        # real state as soon as 021 shows it.
        synth = {"orderId": oid, "time": int(self.client.now()), "token": o21["token"], "exchange": mapping.RESPONSE_EXCHANGE[o21["exchange"]], "product": o21["product"], "qtyRemaining": o21["qty"], "qtyTraded": 0, "price": o21["price"], "triggerPrice": o21.get("trigger") or 0, "book": o21["book"], "validity": o21["validity"], "status": "Received", "reason": "", "placedBy": "User"}
        return self.to_order(synth, att) | {"unconfirmed_read": True}

    def _reconcile_place(self, tag: str, res) -> dict:
        att = store.get(tag)
        fp = self._fp_fields(att["order_021"])
        not_before = self._not_before(att)
        window = self.cfg.reconcile_window_s if res.kind == "unknown" else 0.0
        deadline = time.time() + window
        candidates: list[dict] | None = None
        read_error = None
        polls = 0
        while True:
            polls += 1
            try:
                rows = self.client.get("/orders")
                mapped = store.mapped_order_ids(self.account_id)
                candidates = [o for o in (rows if isinstance(rows, list) else []) if str(o.get("orderId")) not in mapped and mapping.matches(o, fp, not_before)]
                if candidates:
                    break
            except (Api021Error, Api021Unavailable, LoginFailed) as e:
                read_error = str(e)
            if time.time() >= deadline:
                break
            self._sleep(0.7)
        summary = {"tag": tag, "polls": polls, "window_s": window, "original_error": res.error, "http_status": res.status, "kind": res.kind}
        if candidates and len(candidates) == 1:
            row = candidates[0]
            oid = str(row.get("orderId"))
            state = "REJECTED" if row.get("status") == "Rejected" else "ACCEPTED"
            store.update(tag, state=state, order_id=oid, error=res.error, note=f"Reconciled after: {res.error}")
            self.last_reconciliation = summary | {"result": "FOUND", "broker_order_id": oid}
            return self.to_order(row, store.get(tag)) | {"reconciled": True, "original_error": res.error}
        if candidates and len(candidates) > 1:
            ids = [str(o.get("orderId")) for o in candidates]
            store.update(tag, state="AMBIGUOUS", error=res.error, candidates=ids)
            self.last_reconciliation = summary | {"result": "AMBIGUOUS", "candidates": ids}
            raise GatewayError(502, "AMBIGUOUS", f"021's answer was lost ({res.error}) and {len(ids)} orders in the 021 order book match this one ({', '.join(ids)}). Countersign will not guess which is yours and sent nothing more. Review the 021 order book.", {"candidates": ids, "broker_error": res.error, "reconciled": True})
        if candidates is None:
            store.update(tag, state="UNCONFIRMED", error=f"{res.error}; order book unreadable: {read_error}")
            self.last_reconciliation = summary | {"result": "UNCONFIRMED", "read_error": read_error}
            raise GatewayError(502, "UNCONFIRMED", f"021's answer was lost ({res.error}) and its order book could not be read to check ({read_error}). Nothing was resent. The gateway keeps checking; review the 021 order book.", {"broker_error": res.error, "reconciled": False})
        if res.kind == "rejected" or (res.kind == "unknown" and mapping.is_risk_rejection(res.error)):
            store.update(tag, state="REJECTED", error=res.error)
            self.last_reconciliation = summary | {"result": "REJECTED"}
            raise GatewayError(422, "REJECTED_BY_021", res.error or "Rejected by 021.", {"http_status": res.status, "reconciled": True})
        store.update(tag, state="NOT_PLACED", error=res.error)
        self.last_reconciliation = summary | {"result": "NOT_PLACED"}
        what = "refused the access token (HTTP 401)" if res.kind == "auth" else f"answered: {res.error}"
        raise GatewayError(502, "NOT_PLACED", f"021 {what}. The order is not in 021's order book after {window:.0f} s of checking, so it was not placed. Nothing was resent.", {"broker_error": res.error, "http_status": res.status, "reconciled": True})

    # ------------------------------------------------------------------ modify / cancel (verified on the order itself)

    def _verify(self, oid: str, done: Callable[[dict], bool], window: float) -> dict | None:
        end = time.time() + window
        row = None
        while True:
            try:
                row = self.raw_order(oid)
                if done(row) or mapping.order_status(row) in ("COMPLETE", "REJECTED", "CANCELLED"):
                    return row
            except GatewayError:
                pass
            if time.time() >= end:
                return row
            self._sleep(0.5)

    def modify(self, oid: str, quantity: int | None, limit_price: int | None, trigger_price: int | None) -> dict:
        cur = self.raw_order(oid)
        if mapping.order_status(cur) not in OPEN_STATUSES:
            raise GatewayError(409, "ORDER_NOT_OPEN", f"Order {oid} is {cur.get('status')} on 021 and cannot be modified.")
        body = mapping.modify_body(cur, quantity=quantity, price=limit_price, trigger=trigger_price)
        res = self.client.write("PUT", f"/orders/{oid}", body)
        self.invalidate()
        if res.kind == "not_sent":
            raise GatewayError(502, "MODIFY_NOT_SENT", f"{res.error} The order is unchanged; nothing was sent.")

        def applied(r: dict) -> bool:
            return int(r.get("price") or 0) == body["price"] and int(r.get("triggerPrice") or 0) == int(body.get("trigger") or 0) and abs(int(r.get("qtyRemaining") or 0)) + abs(int(r.get("qtyTraded") or 0)) == abs(body["qty"])

        row = self._verify(oid, applied, self.cfg.reconcile_window_s if res.kind != "rejected" else 1.0)
        if row is not None and applied(row):
            return self.to_order(row, store.by_order_id(oid)) | {"modification": "APPLIED", "broker_answer": res.kind}
        if row is not None and mapping.order_status(row) not in OPEN_STATUSES:
            raise GatewayError(409, "ORDER_NOT_OPEN", f"Order {oid} became {row.get('status')} before the modification applied; it was not modified.", {"status_021": row.get("status")})
        if res.kind == "ok":
            return self.to_order(row or cur, store.by_order_id(oid)) | {"modification": "PENDING", "broker_answer": "ok"}
        if res.kind in ("rejected", "auth"):
            raise GatewayError(422, "MODIFY_REJECTED", res.error or "021 refused the modification.", {"http_status": res.status})
        raise GatewayError(502, "MODIFY_UNCONFIRMED", f"021's answer was lost ({res.error}) and the order still shows its old values. Nothing was resent.", {"broker_error": res.error})

    def cancel(self, oid: str) -> dict:
        cur = self.raw_order(oid)
        if mapping.order_status(cur) not in OPEN_STATUSES:
            raise GatewayError(409, "ORDER_NOT_OPEN", f"Order {oid} is {cur.get('status')} on 021 and cannot be cancelled.")
        body = {"exchange": mapping.REQUEST_EXCHANGE.get(cur["exchange"], cur["exchange"]), "token": int(cur["token"]), "product": cur["product"]}
        res = self.client.write("DELETE", f"/orders/{oid}", body)
        self.invalidate()
        if res.kind == "not_sent":
            raise GatewayError(502, "CANCEL_NOT_SENT", f"{res.error} The order is unchanged; nothing was sent.")
        row = self._verify(oid, lambda r: r.get("status") == "Cancelled", self.cfg.reconcile_window_s if res.kind != "rejected" else 1.0)
        if row is not None and row.get("status") == "Cancelled":
            return self.to_order(row, store.by_order_id(oid)) | {"cancellation": "CONFIRMED", "broker_answer": res.kind}
        if row is not None and mapping.order_status(row) in ("COMPLETE", "REJECTED"):
            raise GatewayError(409, "ORDER_NOT_OPEN", f"Order {oid} became {row.get('status')} before the cancellation; it was not cancelled.", {"status_021": row.get("status")})
        if res.kind == "ok" or (row is not None and row.get("status") == "SentForCancellation"):
            return self.to_order(row or cur, store.by_order_id(oid)) | {"cancellation": "PENDING", "broker_answer": res.kind}
        if res.kind in ("rejected", "auth"):
            raise GatewayError(422, "CANCEL_REJECTED", res.error or "021 refused the cancellation.", {"http_status": res.status})
        raise GatewayError(502, "CANCEL_UNCONFIRMED", f"021's answer was lost ({res.error}) and the order is still open. Nothing was resent.", {"broker_error": res.error})

    # ------------------------------------------------------------------ startup recovery

    def recover(self) -> list[dict]:
        """Placements left SENDING by a gateway crash are reconciled (never resent) before serving requests."""
        out = []
        for att in store.stale_sending(self.account_id, 0.0):
            class _R:  # the original answer is unknown: treat as a lost response
                kind, status, error = "unknown", None, "The gateway restarted before 021's answer was recorded."

            try:
                out.append(self._reconcile_place(att["tag"], _R()))
            except GatewayError as e:
                out.append(e.body())
        return out
