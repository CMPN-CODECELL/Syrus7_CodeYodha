"""Deterministic option pricing for the simulated NIFTY chain (Black-Scholes, simple skew)."""

import math
from datetime import UTC, date, datetime

RISK_FREE = 0.065
BASE_IV = 0.13


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def years_to_expiry(expiry: str, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    exp_dt = datetime.combine(date.fromisoformat(expiry), datetime.min.time(), tzinfo=UTC).replace(hour=10)
    secs = max((exp_dt - now).total_seconds(), 3600.0)
    return secs / (365.0 * 86400.0)


def implied_vol(spot: float, strike: float) -> float:
    m = math.log(strike / spot)
    return BASE_IV + 0.9 * m * m + (0.02 if m < 0 else 0.0) * abs(m) * 10


def option_premium_paise(spot_paise: int, strike_paise: int, opt: str, expiry: str, now: datetime | None = None) -> int:
    s = spot_paise / 100.0
    k = strike_paise / 100.0
    t = years_to_expiry(expiry, now)
    v = implied_vol(s, k)
    d1 = (math.log(s / k) + (RISK_FREE + 0.5 * v * v) * t) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    if opt == "CE":
        price = s * _ncdf(d1) - k * math.exp(-RISK_FREE * t) * _ncdf(d2)
    else:
        price = k * math.exp(-RISK_FREE * t) * _ncdf(-d2) - s * _ncdf(-d1)
    price = max(price, 0.05)
    p = int(round(price * 100))
    return max(5, p - p % 5)
