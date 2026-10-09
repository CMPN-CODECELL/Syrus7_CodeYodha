"""Circuit limits the 021 sandbox enforces at runtime but its daily instruments file does not show.

021 publishes circuit limits only in the instruments file (the market packets carry none), and on some days its order
check uses tighter limits than that file (TCS 2026-10-09: file lower 1,872.30, refused "Price 2,090.10 must be higher
than the lower circuit limit 2,104.00"). The gateway learns such a limit from the refusal text and the verifier then
blocks a 021 order outside it before the trader signs.

Storage: one Valkey key per instrument, cs:021:circuit:<our instrument id>, JSON
  {lower_paise, lower_learned_at, lower_source, upper_paise, upper_learned_at, upper_source, learned_at, source}
(either bound may be absent), expiring at the next 08:00 IST, when 021's new trading day and instruments file begin.
Only the gateway writes it (from refusals as they happen, and at every gateway start from the day's stored REJECTED
attempts in g021_attempts, so a flushed or restarted Valkey is rebuilt from Postgres). The API reads it with no 021
credential. A learned limit never widens the band: a lower limit only rises and an upper limit only falls during the
day, and the verifier uses the stricter of the file and the learned value. If Valkey cannot be read the verifier falls
back to the file band (021 itself still refuses the order)."""

from __future__ import annotations

import json
import math
import re
import time
from decimal import Decimal, InvalidOperation

from countersign.core import events

KEY = "cs:021:circuit:{}"
IST_OFFSET_S = 19800
RESET_HOUR_IST = 8

# "must be higher than the lower circuit limit 2,104.00", "should be lower than the Upper Circuit limit: Rs. 2,288.3".
# The verb is not relied on: the bound named next to "circuit" and the number after it are what 021 enforces.
_REFUSAL = re.compile(
    r"\b(lower|upper)[\s_-]*circuit(?:[\s_-]*(?:limit|price|band|level))?[\s:=]*(?:(?:is|of|at)\s+)?(?:(?:rs\.?|inr|₹)\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def parse_refusal(text: str | None) -> dict[str, int]:
    """{"lower": paise} and/or {"upper": paise} named in a 021 circuit refusal; empty when the text is not one. A lower
    limit with sub-paise digits rounds up and an upper one rounds down (never wider than 021 said)."""
    out: dict[str, int] = {}
    for m in _REFUSAL.finditer(text or ""):
        kind = m.group(1).lower()
        try:
            rupees = Decimal(m.group(2).replace(",", ""))
        except InvalidOperation:
            continue
        paise = rupees * 100
        value = math.ceil(paise) if kind == "lower" else math.floor(paise)
        if value > 0 and kind not in out:
            out[kind] = int(value)
    return out


def day_start(now: float | None = None) -> float:
    """The start (08:00 IST) of the 021 trading day that contains now."""
    now = time.time() if now is None else now
    ist = now + IST_OFFSET_S
    start = (ist // 86400) * 86400 + RESET_HOUR_IST * 3600
    if ist < start:
        start -= 86400
    return start - IST_OFFSET_S


def next_reset(now: float | None = None) -> float:
    """When learned limits expire: the next 08:00 IST."""
    return day_start(now) + 86400


def ist_hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts + IST_OFFSET_S))


def merge(cur: dict | None, *, lower: int | None, upper: int | None, at: float, source: str) -> tuple[dict, bool]:
    """The stored limits after learning lower/upper. Never widens: lower only rises, upper only falls."""
    out = dict(cur or {})
    changed = False
    if lower and lower > int(out.get("lower_paise") or 0):
        out.update(lower_paise=int(lower), lower_learned_at=at, lower_source=source)
        changed = True
    if upper and (not out.get("upper_paise") or upper < int(out["upper_paise"])):
        out.update(upper_paise=int(upper), upper_learned_at=at, upper_source=source)
        changed = True
    if changed:
        out.update(learned_at=at, source=source)
    return out, changed


def learn(instrument_id: str, *, lower: int | None = None, upper: int | None = None, at: float | None = None, source: str = "", now: float | None = None) -> dict | None:
    """Records a limit 021 enforced for this instrument today. Returns the stored limits, or None when nothing applies
    (no bound, or the refusal belongs to an earlier trading day)."""
    now = time.time() if now is None else now
    at = now if at is None else min(at, now)
    if not instrument_id or not (lower or upper) or at < day_start(now):
        return None
    key = KEY.format(instrument_id)
    expire_at = int(next_reset(now))
    result: dict = {}

    def _update(pipe) -> None:
        raw = pipe.get(key)
        cur = _decode(raw)
        new, changed = merge(cur, lower=lower, upper=upper, at=at, source=source)
        result.update(new)
        pipe.multi()
        if changed or raw is None:
            pipe.set(key, json.dumps(new), exat=expire_at)
        else:
            pipe.expireat(key, expire_at)

    events.client().transaction(_update, key)
    return result


def learn_from_refusal(instrument_id: str, text: str | None, *, at: float | None = None, source: str = "", now: float | None = None) -> dict | None:
    found = parse_refusal(text)
    if not found:
        return None
    return learn(instrument_id, lower=found.get("lower"), upper=found.get("upper"), at=at, source=source, now=now)


def _decode(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except ValueError:
        return None
    return val if isinstance(val, dict) else None


def learned(instrument_id: str, now: float | None = None) -> dict | None:
    """Today's learned limits for this instrument, or None (none learned, expired, or Valkey unreadable)."""
    try:
        val = _decode(events.client().get(KEY.format(instrument_id)))
    except Exception:  # noqa: BLE001
        return None
    if not val:
        return None
    start = day_start(now)
    out = dict(val)
    for side in ("lower", "upper"):
        if out.get(f"{side}_paise") and float(out.get(f"{side}_learned_at") or 0) < start:
            out.pop(f"{side}_paise", None)  # learned on an earlier trading day: never applies today
    return out if (out.get("lower_paise") or out.get("upper_paise")) else None
