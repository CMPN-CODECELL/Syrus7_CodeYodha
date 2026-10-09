"""Money is always integer paise. Formatting helpers for deterministic summaries."""

from decimal import ROUND_HALF_UP, Decimal


def rupees_to_paise(value: Decimal | str | float | int) -> int:
    d = Decimal(str(value))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def paise_to_rupees(p: int) -> Decimal:
    return (Decimal(p) / 100).quantize(Decimal("0.01"))


def _group_indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def inr(p: int | None, signed: bool = False) -> str:
    if p is None:
        return "-"
    neg = p < 0
    a = abs(p)
    rupees, paise = divmod(a, 100)
    body = f"₹{_group_indian(rupees)}.{paise:02d}"
    if neg:
        return "-" + body
    return ("+" + body) if signed and p > 0 else body


def bps(p_bps: int) -> str:
    return f"{p_bps / 100:.2f}%"


def round_to_tick(price_paise: int, tick_paise: int, direction: str) -> int:
    if tick_paise <= 1:
        return price_paise
    q, r = divmod(price_paise, tick_paise)
    if r == 0:
        return price_paise
    return q * tick_paise if direction == "down" else (q + 1) * tick_paise
