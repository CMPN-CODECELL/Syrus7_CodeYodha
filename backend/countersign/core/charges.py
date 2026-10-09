"""Estimated statutory charges (labelled as estimates). Rates follow common Indian discount-broker schedules."""


def _p(value_paise: int, rate: float) -> int:
    return int(round(value_paise * rate))


def estimate(segment: str, product: str, side: str, value_paise: int) -> tuple[int, list[dict]]:
    rows: list[dict] = []
    if segment == "FO":
        brokerage = 2000
        stt = _p(value_paise, 0.001) if side == "SELL" else 0
        exch = _p(value_paise, 0.0003503)
        stamp = _p(value_paise, 0.00003) if side == "BUY" else 0
    elif product == "CNC":
        brokerage = 0
        stt = _p(value_paise, 0.001)
        exch = _p(value_paise, 0.0000297)
        stamp = _p(value_paise, 0.00015) if side == "BUY" else 0
    else:
        brokerage = min(2000, _p(value_paise, 0.0003))
        stt = _p(value_paise, 0.00025) if side == "SELL" else 0
        exch = _p(value_paise, 0.0000297)
        stamp = _p(value_paise, 0.00003) if side == "BUY" else 0
    sebi = max(1, _p(value_paise, 0.000001))
    gst = _p(brokerage + exch + sebi, 0.18)
    for label, v in (
        ("Brokerage", brokerage),
        ("STT", stt),
        ("Exchange transaction", exch),
        ("SEBI fee", sebi),
        ("Stamp duty", stamp),
        ("GST (18%)", gst),
    ):
        rows.append({"label": label, "paise": v})
    return sum(r["paise"] for r in rows), rows
