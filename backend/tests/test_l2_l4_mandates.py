"""L3 standing instructions: "If Reliance drops below X, buy 10" fires once; "Tell me when any holding falls 3% in a
day" watches every holding, fires at most once per holding per trading day (durable dedupe, so restarts and
concurrent engines never refire) and never places an order."""

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from countersign.core import proposals
from countersign.core.db import session_scope
from countersign.core.mandate_engine import Engine, evaluate, evaluate_holdings_alert, fire_key, trading_day
from countersign.core.market import Tick
from countersign.core.models import ExecJob, OutcomeRow, StandingMandate, SystemState
from countersign.core.parser import parse
from countersign.core.schemas import ALL_HOLDINGS_ID, MandateIntent
from countersign.llm import tools as T
from tests.helpers import approve
from tests.test_l2_l4_support import Gw, account, holding, inst, ltp, rupees, user_id, view


def _mandate(text: str, gw: Gw, intent=None) -> proposals.Outcome:
    mi = intent or parse(text).mandate
    with session_scope() as s:
        out = proposals.create_mandate(s, mi, user_text=text, account=account(gw), user_id=user_id(), engine="deterministic", origin="test")
        return out


def _row(pid: str) -> StandingMandate:
    with session_scope() as s:
        m = s.execute(select(StandingMandate).where(StandingMandate.proposal_id == pid)).scalar_one()
        s.expunge(m)
        return m


def _tick(symbol: str, ltp_paise: int, prev: int, seq: int, ts: datetime | None = None) -> Tick:
    i = inst(symbol)
    return Tick(instrument_id=i.instrument_id, symbol=symbol, ltp_paise=ltp_paise, bid_paise=ltp_paise, ask_paise=ltp_paise, volume=1, prev_close_paise=prev, ts=ts or datetime.now(UTC), seq=seq)


# ---------------------------------------------------------------- 5. "If Reliance drops below X, buy 10"


def test_if_reliance_drops_below_buy_10_fires_once_and_survives_restart():
    p, t = ltp("RELIANCE")
    x = p - 30 * t
    out = _mandate(f"If Reliance drops below {rupees(x)}, buy 10", Gw())
    v = view(out.proposal.id)
    c = out.proposal.canonical
    assert v["status"] == "AWAITING_APPROVAL", v["issues"]
    assert c["predicate"] == {"field": "LTP", "op": "LTE", "threshold": x}
    assert c["template"]["side"] == "BUY" and c["template"]["quantity"] == 10 and c["template"]["limit_price_paise"] == x
    assert c["one_shot"] is True and c["max_quantity"] == 10 and "scope" not in c
    approve(out.proposal.id)
    mid = _row(out.proposal.id).id
    with session_scope() as s:
        assert evaluate(s, s.get(StandingMandate, mid), _tick("RELIANCE", p, p, 1)) == "NONE"
    with session_scope() as s:
        assert evaluate(s, s.get(StandingMandate, mid), _tick("RELIANCE", x - t, p, 2)) == "TRIGGERED"
    with session_scope() as s:
        assert evaluate(s, s.get(StandingMandate, mid), _tick("RELIANCE", x - 2 * t, p, 3)) == "NONE"
    Engine("after-restart").refresh()
    with session_scope() as s:
        assert evaluate(s, s.get(StandingMandate, mid), _tick("RELIANCE", x - 3 * t, p, 4)) == "NONE"
        assert s.execute(select(func.count()).select_from(ExecJob).where(ExecJob.mandate_id == mid)).scalar_one() == 1


def test_drop_phrasings_sign_the_same_instruction():
    p, t = ltp("RELIANCE")
    x = rupees(p - 30 * t)
    cans = []
    for text in (f"If Reliance drops below {x}, buy 10", f"If Reliance goes below {x}, buy 10", f"If Reliance falls under {x}, buy 10", f"Buy 10 Reliance if it drops below {x}"):
        c = dict(_mandate(text, Gw()).proposal.canonical)
        cans.append((c["predicate"], c["template"]["side"], c["template"]["quantity"], c["template"]["limit_price_paise"], c["instrument"]["instrument_id"]))
    assert len(set(map(str, cans))) == 1


# ---------------------------------------------------------------- 6. "Tell me when any holding falls 3% in a day"


def _holdings_gw() -> tuple[Gw, dict[str, int]]:
    prices = {s: ltp(s)[0] for s in ("INFY", "ABCCORP")}
    return Gw(holdings=[holding("INFY", 100, prices["INFY"]), holding("ABCCORP", 50, prices["ABCCORP"])]), prices


def _armed_alert() -> tuple[str, dict[str, dict], dict[str, int]]:
    gw, prices = _holdings_gw()
    out = _mandate("Tell me when any holding falls 3% in a day", gw)
    approve(out.proposal.id)
    m = _row(out.proposal.id)
    assert m.state == "ACTIVE"
    return m.id, {h["instrument_id"]: h for h in gw.holdings()}, prices


def test_holdings_alert_is_signed_with_its_scope_and_firing_policy():
    gw, _ = _holdings_gw()
    out = _mandate("Tell me when any holding falls 3% in a day", gw)
    c = out.proposal.canonical
    assert c["kind"] == "ALERT" and c["scope"] == "ALL_HOLDINGS" and c["fire_policy"] == "ONCE_PER_HOLDING_PER_DAY"
    assert c["predicate"] == {"field": "DAY_CHANGE_BPS", "op": "LTE", "threshold": -300}
    assert c["template"] is None and c["one_shot"] is False and c["instrument"]["instrument_id"] == ALL_HOLDINGS_ID
    v = view(out.proposal.id)
    assert v["status"] == "AWAITING_APPROVAL" and "once per holding per trading day" in v["summary"]
    assert v["mandate"]["condition_text"] == "any holding is down 3.00% or more today (vs previous close)"
    m = _row(out.proposal.id)
    assert m.one_shot is False and m.instrument_id == ALL_HOLDINGS_ID and m.kind == "ALERT"


def test_fires_once_per_holding_per_day_across_restarts_and_never_orders():
    mid, held, prices = _armed_alert()
    pi, pa = prices["INFY"], prices["ABCCORP"]
    down = lambda prev, pct_bps: prev - prev * pct_bps // 10000  # noqa: E731
    now = datetime.now(UTC)
    with session_scope() as s:
        assert evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("INFY", down(pi, 250), pi, 1), held) == "NONE"
    with session_scope() as s:
        assert evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("INFY", down(pi, 350), pi, 2), held) == "TRIGGERED"
    with session_scope() as s:
        assert evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("INFY", down(pi, 400), pi, 3), held) == "DUPLICATE"
    Engine("after-restart").refresh()
    with session_scope() as s:
        assert evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("INFY", down(pi, 500), pi, 4), held) == "DUPLICATE"
    with session_scope() as s:
        assert evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("ABCCORP", down(pa, 300), pa, 5), held) == "TRIGGERED"
    with session_scope() as s:
        assert evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("INFY", down(pi, 350), pi, 6, ts=now + timedelta(days=1)), held) == "TRIGGERED"
    tcs, _ = ltp("TCS")
    with session_scope() as s:
        assert evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("TCS", down(tcs, 900), tcs, 7), held) == "NONE"
    with session_scope() as s:
        m = s.get(StandingMandate, mid)
        assert m.state == "ACTIVE" and m.fired_at is not None
        assert s.execute(select(func.count()).select_from(ExecJob)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(OutcomeRow).where(OutcomeRow.kind == "ALERT")).scalar_one() == 3
        day = trading_day(_tick("INFY", pi, pi, 1, ts=now))
        assert s.get(SystemState, fire_key(mid, inst("INFY").instrument_id, day)) is not None


def test_boundary_is_exact_three_percent():
    mid, held, prices = _armed_alert()
    pi = 100000
    held = {k: dict(v, prev_close_paise=pi) for k, v in held.items()}
    with session_scope() as s:
        assert evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("INFY", 97001, pi, 1), held) == "NONE"
    with session_scope() as s:
        assert evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("INFY", 97000, pi, 2), held) == "TRIGGERED"


def test_concurrent_engines_fire_a_holding_once():
    mid, held, prices = _armed_alert()
    pi = prices["INFY"]
    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        with session_scope() as s:
            results.append(evaluate_holdings_alert(s, s.get(StandingMandate, mid), _tick("INFY", pi - pi * 4 // 100, pi, 10 + i), held, engine_id=f"engine-{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count("TRIGGERED") == 1 and results.count("DUPLICATE") == 7


def test_engine_tick_path_uses_cached_holdings_and_dedupes(monkeypatch):
    mid, held, prices = _armed_alert()
    pi = prices["INFY"]
    eng = Engine("engine-test")
    calls: list[str] = []
    monkeypatch.setattr(eng, "holdings_for", lambda acct: calls.append(acct) or held)
    eng.refresh()
    for seq in (1, 2, 3):
        eng.on_tick(_tick("INFY", pi - pi * 5 // 100, pi, seq))
    fresh = Engine("engine-after-restart")
    monkeypatch.setattr(fresh, "holdings_for", lambda acct: held)
    fresh.on_tick(_tick("INFY", pi - pi * 6 // 100, pi, 4))
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(OutcomeRow).where(OutcomeRow.kind == "ALERT")).scalar_one() == 1
    assert calls and set(calls) == {"LAB1"}


def test_holdings_alert_scope_and_percentage_must_be_stated():
    gw, _ = _holdings_gw()
    wide = MandateIntent(kind="ALERT", scope="ALL_HOLDINGS", field="DAY_CHANGE_BPS", op="LTE", drop_pct=3)
    v = view(_mandate("Alert me if INFY falls 3% today", gw, intent=wide).proposal.id)
    assert "SCOPE_NOT_STATED" in {i["code"] for i in v["issues"] if i["severity"] == "BLOCK"}
    five = MandateIntent(kind="ALERT", scope="ALL_HOLDINGS", field="DAY_CHANGE_BPS", op="LTE", drop_pct=5)
    v = view(_mandate("Tell me when any holding falls 3% in a day", gw, intent=five).proposal.id)
    assert "THRESHOLD_NOT_STATED" in {i["code"] for i in v["issues"] if i["severity"] == "BLOCK"}
    cards: list[dict] = []
    d = T.ChatDeps(user_id=user_id(), account_id="LAB1", conversation_id="c-a", user_text="Tell me when any holding falls 3% in a day", engine="groq:test", emit=cards.append, origin="test", gateway=gw)
    r = T.propose_standing_instruction(d, "ALERT", None, "ANY_HOLDING_DAY_DROP", drop_pct=3)
    assert r["status"] == "AWAITING_APPROVAL", r
    order = MandateIntent(kind="ORDER", scope="ALL_HOLDINGS", field="DAY_CHANGE_BPS", op="LTE", drop_pct=3)
    out = _mandate("Tell me when any holding falls 3% in a day", gw, intent=order)
    assert out.proposal is None and "not supported" in out.clarify[0]
