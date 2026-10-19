"""
Pure math for the weather bot: no network calls here, so it's easy to test.

The idea: each ensemble member is one plausible version of tomorrow. We take each
member's high (or low) over the official NWS "climate day", nudge it by the
station's measured bias, put a small bell curve around it, and add them all up.
The share of that combined curve inside a bracket is the bracket's probability.
"""
import math
import re
from datetime import datetime, timedelta, timezone

import weather_config as wc

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


# ---------------------------------------------------------------------------
# Dates and the NWS climate day
# ---------------------------------------------------------------------------
def event_date(event_ticker):
    """'KXHIGHNY-26OCT19' -> '2026-10-19' (None if it doesn't look like a dated event)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(?:$|-)", event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    return f"20{m.group(1)}-{MONTHS[m.group(2)]:02d}-{m.group(3)}"


def us_dst_active(utc_ts, std_offset_hours):
    """US daylight time: 2nd Sunday of March 2am -> 1st Sunday of November 2am (local)."""
    local = datetime.fromtimestamp(utc_ts, timezone.utc) + timedelta(hours=std_offset_hours)
    y = local.year

    def nth_sunday(month, n):
        d = datetime(y, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    start = nth_sunday(3, 2).replace(hour=2)
    end = nth_sunday(11, 1).replace(hour=1)   # 2am daylight = 1am standard
    naive = local.replace(tzinfo=None)
    return start <= naive < end


def local_clock(utc_ts, station):
    """Local wall-clock datetime (naive) at the station, DST-aware."""
    off = station["std_offset"] + (1 if station.get("dst", True) and
                                   us_dst_active(utc_ts, station["std_offset"]) else 0)
    return datetime.fromtimestamp(utc_ts, timezone.utc).replace(tzinfo=None) + timedelta(hours=off)


def climate_day_window(date_str, std_offset_hours):
    """NWS climate reports use local STANDARD time all year: midnight-to-midnight LST.
    (During daylight time that's 1:00 AM to 12:59 AM on the local clock.)"""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = d - timedelta(hours=std_offset_hours)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def window_extreme(times, values, start_ts, end_ts, kind, min_hours=20):
    """Max (high) or min (low) of hourly values inside [start, end). None if too sparse."""
    pts = [v for t, v in zip(times, values) if start_ts <= t < end_ts and v is not None]
    if len(pts) < min_hours:
        return None
    return max(pts) if kind == "high" else min(pts)


# ---------------------------------------------------------------------------
# Brackets
# ---------------------------------------------------------------------------
def bracket_bounds(market):
    """Continuous temperature range a market's YES side covers.
    Official values are whole degrees, so '64-65°' means [63.5, 65.5).
    Kalshi: greater 67 -> 68° or above; less 60 -> 59° or below; between 64/65 inclusive."""
    st = market.get("strike_type")
    fl, cap = market.get("floor_strike"), market.get("cap_strike")
    try:
        if st == "greater" and fl is not None:
            return float(fl) + 0.5, math.inf
        if st == "greater_or_equal" and fl is not None:
            return float(fl) - 0.5, math.inf
        if st == "less" and cap is not None:
            return -math.inf, float(cap) - 0.5
        if st == "less_or_equal" and cap is not None:
            return -math.inf, float(cap) + 0.5
        if st == "between" and fl is not None and cap is not None:
            return float(fl) - 0.5, float(cap) + 0.5
    except (TypeError, ValueError):
        return None
    return None


def bracket_label(lo, hi):
    if lo == -math.inf:
        return f"≤{int(hi - 0.5)}°"
    if hi == math.inf:
        return f"≥{int(lo + 0.5)}°"
    a, b = int(lo + 0.5), int(hi - 0.5)
    return f"{a}°" if a == b else f"{a}–{b}°"


# ---------------------------------------------------------------------------
# Probability
# ---------------------------------------------------------------------------
def _cdf(x, mu, sd):
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    return 0.5 * (1 + math.erf((x - mu) / (sd * math.sqrt(2))))


def build_components(extremes, bias, kernel_sd, nbm_value=None):
    """extremes: {"ecmwf": [..members..], "gfs": [...]} -> list of (center, sd, weight, model)."""
    comps = []
    avail = {k: v for k, v in (extremes or {}).items() if v}
    weights = {k: wc.MODEL_WEIGHTS.get(k, 0) for k in avail}
    if nbm_value is not None:
        weights["nbm"] = wc.MODEL_WEIGHTS.get("nbm", 0)
    total = sum(weights.values())
    if total <= 0:
        return comps
    for model, members in avail.items():
        w = weights[model] / total / len(members)
        comps += [(m + bias, kernel_sd, w, model) for m in members]
    if nbm_value is not None and weights.get("nbm"):
        comps.append((nbm_value + bias, wc.NBM_SD_F, weights["nbm"] / total, "nbm"))
    return comps


def prob_in(components, lo, hi, kind="high", observed=None, models=None):
    """P(official value in [lo, hi)). If we already observed today's high so far, the final
    high can't be lower (and a low can't be higher), so each bell curve is cut off there."""
    num = den = 0.0
    bound = None
    if observed is not None:
        bound = observed - wc.OBS_TOLERANCE_F if kind == "high" else observed + wc.OBS_TOLERANCE_F
    for mu, sd, w, model in components:
        if models and model not in models:
            continue
        if bound is None:
            p, z = _cdf(hi, mu, sd) - _cdf(lo, mu, sd), 1.0
        elif kind == "high":
            z = 1 - _cdf(bound, mu, sd)
            if z < 1e-12:   # whole curve below what we've already seen: it's at the bound
                p, z = (1.0 if lo <= bound < hi else 0.0), 1.0
            else:
                p = max(0.0, _cdf(hi, mu, sd) - _cdf(max(lo, bound), mu, sd)) if hi > bound else 0.0
        else:
            z = _cdf(bound, mu, sd)
            if z < 1e-12:
                p, z = (1.0 if lo <= bound < hi else 0.0), 1.0
            else:
                p = max(0.0, _cdf(min(hi, bound), mu, sd) - _cdf(lo, mu, sd)) if lo < bound else 0.0
        num += w * p / z
        den += w
    return num / den if den else None


def summary(components):
    """Weighted mean and 10th/90th percentiles of the forecast, for the dashboard."""
    if not components:
        return None
    tot = sum(c[2] for c in components)
    mean = sum(c[0] * c[2] for c in components) / tot
    pts = sorted((c[0], c[2] / tot) for c in components)
    acc, p10, p90 = 0.0, pts[0][0], pts[-1][0]
    for v, w in pts:
        acc += w
        if acc >= 0.10 and p10 == pts[0][0]:
            p10 = v
        if acc >= 0.90:
            p90 = v
            break
    return {"mean": round(mean, 1), "p10": round(p10, 1), "p90": round(p90, 1)}


def kelly(q, price):
    """Kelly fraction for buying a contract at `price` that wins with probability q."""
    if price >= 1 or q <= price:
        return 0.0
    return (q - price) / (1 - price)


def calibrate(pairs):
    """pairs: [(forecast, actual)] -> {'bias', 'sd', 'n'} (bias = mean(actual - forecast))."""
    errs = [a - f for f, a in pairs if f is not None and a is not None]
    if len(errs) < 5:
        return None
    b = sum(errs) / len(errs)
    sd = math.sqrt(sum((e - b) ** 2 for e in errs) / (len(errs) - 1))
    return {"bias": round(b, 2), "sd": round(sd, 2), "n": len(errs)}
