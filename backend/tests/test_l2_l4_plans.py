"""L4 plans computed by the verifier: exit every losing intraday position, and a sells-only rebalance to a maximum
weight. One signature binds all steps; steps are independent; execution reports each step exactly and never makes
a compensating trade."""

from fractions import Fraction

from sqlalchemy import select

from countersign.core import proposals
from countersign.core.broker_client import BrokerError
from countersign.core.db import session_scope
from countersign.core.models import Plan, PlanStep
from countersign.core.portfolio import rebalance_sells
from countersign.core.schemas import Side
from countersign.core.verifier import protection_price
from countersign.llm import tools as T
from tests.conftest import FakeBroker
from tests.helpers import approve, job_for
from tests.test_l2_l4_support import Gw, account, holding, ltp, open_order, position, proposal_count, user_id, view


def _exit(text: str, gw: Gw) -> proposals.Outcome:
    with session_scope() as s:
        return proposals.create_exit_plan(s, user_text=text, account=account(gw), user_id=user_id(), engine="deterministic", origin="test")


def _rebalance(text: str, gw: Gw, max_pct=None) -> proposals.Outcome:
    with session_scope() as s:
        return proposals.create_rebalance_plan(s, user_text=text, account=account(gw), user_id=user_id(), engine="deterministic", max_pct=max_pct, origin="test")


def _book():
    """ICICIBANK long 40 losing, SBIN short 20 losing, INFY long 10 winning (all MIS), TCS NRML long 5 losing."""
    pi, ti = ltp("ICICIBANK")
    ps, ts = ltp("SBIN")
    pf, tf = ltp("INFY")
    pt, tt = ltp("TCS")
    return Gw(
        positions=[
            position("ICICIBANK", "MIS", 40, pi + 10 * ti),
            position("SBIN", "MIS", -20, ps - 4 * ts),
            position("INFY", "MIS", 10, pf - 20 * tf),
            position("TCS", "NRML", 5, pt + 10 * tt),
        ]
    ), (pi, ti), (ps, ts)


# ---------------------------------------------------------------- 7. exit all losing intraday positions


def test_exit_losing_intraday_positions_is_one_plan_of_independent_exact_exits():
    gw, (pi, ti), (ps, ts) = _book()
    out = _exit("Exit all my losing intraday positions", gw)
    v = view(out.proposal.id)
    assert v["kind"] == "PLAN" and v["status"] == "AWAITING_APPROVAL", v["issues"]
    steps = [st["canonical"] for st in v["steps"]]
    assert [(c["instrument"]["symbol"], c["side"], c["quantity"], c["product"], c["order_type"]) for c in steps] == [
        ("ICICIBANK", "SELL", 40, "MIS", "MARKET"),
        ("SBIN", "BUY", 20, "MIS", "MARKET"),
    ]
    assert steps[0]["protection_price_paise"] == protection_price(Side.SELL, pi, ti)
    assert steps[1]["protection_price_paise"] == protection_price(Side.BUY, ps, ts)
    assert all(st["depends_on"] is None for st in v["steps"])
    f0 = {f["key"]: f for f in v["steps"][0]["fields"]}
    assert f0["quantity"]["source"] == "account" and "square-off quantity" in f0["quantity"]["note"]
    assert f0["unrealised_pnl"]["display"].startswith("-")
    assert f0["order_type"]["source"] == "system_default" and "price-protected" in f0["order_type"]["note"]
    assert "EXIT_SCOPE" in {i["code"] for i in v["steps"][0]["issues"]}
    assert "PLAN_USES_PROCEEDS" not in {i["code"] for i in v["issues"]}
    with session_scope() as s:
        assert s.get(Plan, out.proposal.id).state == "AWAITING_APPROVAL"
        rows = s.execute(select(PlanStep).where(PlanStep.plan_id == out.proposal.id).order_by(PlanStep.step_no)).scalars().all()
        assert [r.depends_on for r in rows] == [None, None] and all(r.state == "PENDING" for r in rows)


def test_exit_without_intraday_includes_nrml_but_never_holdings():
    gw, _, _ = _book()
    gw._h = [holding("ITC", 100, 10_00_000)]
    ltp("ITC")
    v = view(_exit("Exit all my losing positions", gw).proposal.id)
    syms = [st["canonical"]["instrument"]["symbol"] for st in v["steps"]]
    assert sorted(syms) == ["ICICIBANK", "SBIN", "TCS"]


def test_nothing_to_exit_is_explained_and_nothing_is_drafted():
    pf, tf = ltp("INFY")
    out = _exit("Exit all my losing intraday positions", Gw(positions=[position("INFY", "MIS", 10, pf - 20 * tf)]))
    assert out.proposal is None and "None of your intraday (MIS) positions is losing" in out.notice[1] and "INFY" in out.notice[1]
    out = _exit("Exit all my losing intraday positions", Gw())
    assert out.proposal is None and "no open intraday (MIS) positions" in out.notice[1]
    assert proposal_count() == 0


def test_exit_plan_requires_the_trader_to_ask_for_it():
    gw, _, _ = _book()
    cards: list[dict] = []
    d = T.ChatDeps(user_id=user_id(), account_id="LAB1", conversation_id="c-x", user_text="How are my positions doing?", engine="groq:test", emit=cards.append, origin="test", gateway=gw)
    r = T.propose_exit_losing_positions(d)
    assert r["status"] == "NOTHING_DRAFTED" and proposal_count() == 0
    assert cards[-1]["kind"] == "notice"


def test_unpriced_positions_and_open_orders_are_reported_on_the_card():
    gw, (pi, ti), _ = _book()
    gw._p.append(position("ITC", "MIS", 10, 10_00_000))
    gw._o.append(open_order("555001", "ICICIBANK", "SELL", 40, "SL", trigger=pi - 20 * ti, limit=pi - 21 * ti, product="MIS"))
    from countersign.core import events

    events.client().delete("cs:tick:NSE:EQ:ITC")
    v = view(_exit("Exit all my losing intraday positions", gw).proposal.id)
    codes0 = {i["code"]: i for i in v["steps"][0]["issues"]}
    assert "OPEN_ORDERS_REMAIN" in codes0 and "555001" in codes0["OPEN_ORDERS_REMAIN"]["message"]
    assert "EXIT_UNPRICED" in codes0 and "ITC" in codes0["EXIT_UNPRICED"]["message"]


class _Scripted(FakeBroker):
    """Fills step 1 partially, rejects step 2 at the broker; anything after must never be sent."""

    def place(self, body):
        with self.lock:
            n = len(self.book)
            o = self._new(body)
            if n == 0:
                o.update(status="PARTIALLY_FILLED", filled_quantity=15, pending_quantity=body["quantity"] - 15, avg_fill_price_paise=body.get("protection_price_paise"))
                return dict(o)
            self.book.pop()
            raise BrokerError(422, "RMS_REJECTED", "RMS: margin shortfall for this order (simulated)")


def test_exit_plan_execution_reports_each_step_exactly_and_never_compensates():
    from countersign.executor import pipeline

    gw, _, _ = _book()
    pf, tf = ltp("INFY")
    gw._p[2] = position("INFY", "MIS", 10, pf + 20 * tf)  # a third losing position
    out = _exit("Exit all my losing intraday positions", gw)
    pid = out.proposal.id
    assert len(view(pid)["steps"]) == 3
    fb = _Scripted()
    pipeline._brokers.clear()
    pipeline._brokers["LAB1"] = fb
    try:
        approve(pid, hold=True)
        job = job_for(pid)
        from sqlalchemy import text

        with session_scope() as s:
            s.execute(text("UPDATE exec_jobs SET status='PENDING' WHERE id=:id"), {"id": job.id})
        pre = pipeline.preflight(job.id)
        assert pre["ok"], pre
        # The same loop as the executor's DBOS workflow: stop on failure, skip the rest, no compensation.
        for step in pre["steps"]:
            r = pipeline.send_step(job.id, step)
            if not r.get("ok"):
                pipeline.skip_remaining(job.id, step["step_no"] + 1, f"Step {step['step_no']} did not succeed; stop on failure (no compensating trades).")
                break
        pipeline.finalize(job.id)
    finally:
        pipeline._brokers.clear()
    v = view(pid)
    states = [(st["step_no"], st.get("state"), st.get("filled_quantity")) for st in v["steps"]]
    assert states[0] == (1, "PARTIALLY_FILLED", 15)
    assert states[1][1] == "REJECTED" and "margin shortfall" in (v["steps"][1].get("reason") or "")
    assert states[2][1] == "SKIPPED"
    assert len(fb.book) == 1 and fb.book[0]["side"] == "SELL" and fb.book[0]["quantity"] == 40


# ---------------------------------------------------------------- 8. rebalance to a maximum weight


def _concentrated() -> tuple[Gw, dict[str, int]]:
    qty = {"INFY": 200, "TCS": 20, "ITC": 300, "SBIN": 100, "HDFCBANK": 50, "RELIANCE": 40}
    prices = {sym: ltp(sym)[0] for sym in qty}
    return Gw(holdings=[holding(sym, q, prices[sym]) for sym, q in qty.items()]), prices


def _weights(qty: dict[str, int], prices: dict[str, int]) -> dict[str, Fraction]:
    v = sum(qty[k] * prices[k] for k in qty)
    return {k: Fraction(qty[k] * prices[k] * 100, v) for k in qty}


def test_rebalance_sells_only_and_every_weight_ends_within_the_cap():
    gw, prices = _concentrated()
    start = {h["symbol"]: h["quantity"] for h in gw.holdings()}
    before = _weights(start, prices)
    assert max(before.values()) > 20
    out = _rebalance("Rebalance so no single stock is more than 20% of my portfolio", gw)
    v = view(out.proposal.id)
    assert v["kind"] == "PLAN" and v["status"] == "AWAITING_APPROVAL", v["issues"]
    sells = {st["canonical"]["instrument"]["symbol"]: st["canonical"]["quantity"] for st in v["steps"]}
    assert all(st["canonical"]["side"] == "SELL" and st["canonical"]["product"] == "CNC" for st in v["steps"])
    assert all(st["depends_on"] is None for st in v["steps"])
    after_qty = {k: start[k] - sells.get(k, 0) for k in start}
    after = _weights(after_qty, prices)
    assert max(after.values()) <= 20
    for k, n in sells.items():
        # Minimal: one share fewer of this stock and some stock would be above the cap.
        less = dict(after_qty, **{k: after_qty[k] + 1})
        assert max(_weights(less, prices).values()) > 20, k
    order = [st["canonical"]["instrument"]["symbol"] for st in v["steps"]]
    assert order == sorted(order, key=lambda s: before[s], reverse=True)
    notes = {i["code"]: i["message"] for i in v["steps"][0]["issues"]}
    assert "holdings only" in notes["REBALANCE_METHOD"] and "Sells only" in notes["REBALANCE_METHOD"]
    assert "→" in notes["REBALANCE_RESULT"]
    f0 = {f["key"]: f for f in v["steps"][0]["fields"]}
    assert f0["quantity"]["note"].startswith("Computed from your holdings") and "→" in f0["weight"]["display"]


def test_rebalance_explains_when_there_is_nothing_to_do_or_it_cannot_be_done():
    gw, _ = _concentrated()
    out = _rebalance("Rebalance so no single stock is more than 50% of my portfolio", gw)
    assert out.proposal is None and "Already within the limit" in out.notice[1]
    three = Gw(holdings=[holding(s, 10, ltp(s)[0]) for s in ("INFY", "TCS", "ITC")])
    out = _rebalance("Rebalance so no single stock is more than 20% of my portfolio", three)
    assert out.proposal is None and "3 x 20% is less than 100%" in out.notice[1]
    from countersign.core import events

    events.client().delete("cs:tick:NSE:EQ:TCS")
    out = _rebalance("Rebalance so no single stock is more than 20% of my portfolio", gw)
    assert out.proposal is None and "live price" in out.notice[1] and "TCS" in out.notice[1]
    assert proposal_count() == 0


def test_rebalance_cap_must_be_the_one_the_trader_stated():
    gw, _ = _concentrated()
    out = _rebalance("Rebalance so no single stock is more than 20% of my portfolio", gw, max_pct=25)
    assert out.proposal is None and out.notice[0] == "Misread guard"
    out = _rebalance("Show my holdings", gw, max_pct=20)
    assert out.proposal is None and "does not ask to rebalance" in out.notice[1]
    assert proposal_count() == 0


def test_rebalance_math_with_lots_and_exact_bounds():
    rows = [{"key": "A", "qty": 1000, "price": 100, "lot": 10}, {"key": "B", "qty": 100, "price": 100, "lot": 1}, {"key": "C", "qty": 100, "price": 100, "lot": 1}, {"key": "D", "qty": 100, "price": 100, "lot": 1}, {"key": "E", "qty": 100, "price": 100, "lot": 1}, {"key": "F", "qty": 100, "price": 100, "lot": 1}]
    r = rebalance_sells(rows, 2000)
    assert r["status"] == "PLAN" and set(r["sells"]) == {"A"} and r["sells"]["A"] % 10 == 0
    assert all(w <= 2000 for w in r["after"].values())
    assert max(r["after"].values()) == r["after"]["A"]
    assert rebalance_sells(rows, 9000)["status"] == "WITHIN"
    assert rebalance_sells(rows[:2], 2000)["status"] == "INFEASIBLE"
    eq = [{"key": k, "qty": 10, "price": 100, "lot": 1} for k in "ABCDE"]
    assert rebalance_sells(eq, 2000)["status"] == "WITHIN"
    big = [{"key": "A", "qty": 3, "price": 100, "lot": 5}] + [{"key": k, "qty": 1, "price": 100, "lot": 1} for k in "BCDEF"]
    r = rebalance_sells(big, 2000)
    assert r["status"] == "PLAN" and r["sells"]["A"] <= 3
