"""
The "favorite harvester" strategy: which markets qualify, and how to rank them.

Idea in one breath: research on 300k+ Kalshi contracts found that strong
favorites win slightly more often than their price implies (the
favorite-longshot bias), and prices get more accurate near close. So we buy
the 80-95c side of liquid, tight, short-dated markets and hold to settlement.
"""
import math
import re
from collections import Counter
from datetime import datetime, timezone

import config


# ---------------------------------------------------------------------------
# Reading Kalshi's market objects
# ---------------------------------------------------------------------------
def dollars(m, key):
    """Price field in dollars. Uses the current '<key>_dollars' string field,
    falling back to the legacy integer-cents field if it's ever present."""
    v = m.get(key + "_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    v = m.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 100.0
    return None


def count(m, key):
    """Contract-count field ('<key>_fp' string, legacy int fallback)."""
    v = m.get(key + "_fp")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    v = m.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def parse_ts(iso):
    if not iso:
        return None
    s = iso.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        # older Pythons choke on 7+ fractional digits; trim them
        s = re.sub(r"\.(\d{6})\d+", r".\1", s)
        s = re.sub(r"\.(\d{1,5})(?=[+-])", lambda mm: "." + mm.group(1).ljust(6, "0"), s)
        return datetime.fromisoformat(s).timestamp()


def series_of(m):
    return (m.get("event_ticker") or m.get("ticker") or "").split("-")[0]


def label_of(m):
    title = m.get("title") or m.get("event_ticker") or m.get("ticker")
    sub = m.get("yes_sub_title") or ""
    return f"{title} — {sub}" if sub and sub not in title else title


_GROUP_PATTERNS = None


def related_group(text):
    """Name of the related-market group this title belongs to, or None."""
    global _GROUP_PATTERNS
    if _GROUP_PATTERNS is None or set(_GROUP_PATTERNS) != set(config.RELATED_GROUPS):
        _GROUP_PATTERNS = {k: re.compile(v) for k, v in config.RELATED_GROUPS.items()}
    t = (text or "").lower()
    for name, pat in _GROUP_PATTERNS.items():
        if pat.search(t):
            return name
    return None


def _valid(p):
    return p is not None and 0 < p < 1


def side_quote(m, side):
    """Best ask/bid for buying YES or NO, plus how many contracts sit at the ask.
    In a binary market, a NO ask is just a YES bid seen from the other side."""
    yb, ya = dollars(m, "yes_bid"), dollars(m, "yes_ask")
    if side == "yes":
        ask, bid = ya, yb
        ask_size = count(m, "yes_ask_size")
    else:
        ask = dollars(m, "no_ask")
        if not _valid(ask) and _valid(yb):
            ask = round(1 - yb, 4)
        bid = dollars(m, "no_bid")
        if not _valid(bid) and _valid(ya):
            bid = round(1 - ya, 4)
        ask_size = count(m, "yes_bid_size")
    pb, pa = dollars(m, "previous_yes_bid"), dollars(m, "previous_yes_ask")
    prev_yes = (pb + pa) / 2 if _valid(pb) and _valid(pa) else None
    prev = None if prev_yes is None else (prev_yes if side == "yes" else 1 - prev_yes)
    return {
        "ask": ask if _valid(ask) else None,
        "bid": bid if _valid(bid) else None,
        "ask_size": ask_size,
        "prev_mid": prev,
    }


def settlement_payout(m, side):
    """$ per contract our side receives once the market is decided, else None."""
    status = m.get("status") or ""
    if status not in ("determined", "finalized", "amended", "settled"):
        return None
    result = (m.get("result") or "").lower()
    if result == "yes":
        yes_value = 1.0
    elif result == "no":
        yes_value = 0.0
    else:
        yes_value = dollars(m, "settlement_value")
        if yes_value is None:
            return None
    return yes_value if side == "yes" else round(1 - yes_value, 4)


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------
def fee_per_contract(price):
    """Unrounded taker fee for one contract (used for screening)."""
    return config.TAKER_FEE_RATE * price * (1 - price)


def taker_fee(contracts, price):
    """Kalshi taker fee for a whole order, rounded UP to the next cent."""
    raw_cents = config.TAKER_FEE_RATE * contracts * price * (1 - price) * 100
    return math.ceil(raw_cents - 1e-9) / 100.0


# ---------------------------------------------------------------------------
# Screening and ranking
# ---------------------------------------------------------------------------
def evaluate(m, now_ts):
    """Return (candidate_dict, None) if the market qualifies, else (None, reason)."""
    ticker = m.get("ticker", "")
    if (m.get("market_type") or "binary") != "binary":
        return None, "not a yes/no market"
    if m.get("mve_collection_ticker") or any(
        ticker.startswith(p) or series_of(m).startswith(p) for p in config.EXCLUDE_TICKER_PREFIXES
    ):
        return None, "excluded series (combo/parlay)"

    close_ts = parse_ts(m.get("close_time"))
    if close_ts is None:
        return None, "missing close time"
    hours = (close_ts - now_ts) / 3600
    if not (config.MIN_HOURS_TO_CLOSE <= hours <= config.MAX_HOURS_TO_CLOSE):
        return None, "outside time window"

    vol24 = count(m, "volume_24h")
    if vol24 < config.MIN_VOLUME_24H:
        return None, "too little volume"

    side, q = None, None
    for s in ("yes", "no"):
        sq = side_quote(m, s)
        if sq["ask"] is not None and config.MIN_PRICE <= sq["ask"] <= config.MAX_PRICE:
            side, q = s, sq
            break
    if side is None:
        return None, "no favorite in 80-95c band"

    price = q["ask"]
    spread = price - (q["bid"] or 0.0)
    if spread > config.MAX_SPREAD + 1e-9:
        return None, "spread too wide"
    if q["ask_size"] < 1:
        return None, "nothing for sale at the ask"
    all_in = price + fee_per_contract(price)
    if all_in > config.MAX_ALL_IN_COST:
        return None, "fees eat the profit"
    mid_now = (price + q["bid"]) / 2 if q["bid"] else price
    if q["prev_mid"] is not None and q["prev_mid"] - mid_now > config.MAX_DAILY_DROP:
        return None, "favorite is slipping fast"

    # Ranking: we don't pretend to know which favorite will win. We prefer the
    # prices most likely to be *accurate*: close to settlement, heavily traded,
    # tight spread, with a mild tilt toward stronger favorites.
    time_score = 1 / (1 + hours / 6)
    liq_score = min(1.0, math.log10(1 + vol24) / 4)          # ~10k contracts/day = full marks
    spread_score = max(0.0, 1 - spread / (config.MAX_SPREAD + 0.01))
    safety = (price - config.MIN_PRICE) / (config.MAX_PRICE - config.MIN_PRICE)
    score = 0.35 * time_score + 0.30 * liq_score + 0.20 * spread_score + 0.15 * safety

    return {
        "ticker": ticker,
        "event_ticker": m.get("event_ticker") or ticker,
        "series": series_of(m),
        "label": label_of(m),
        "group": related_group(label_of(m)),
        "side": side,
        "price": round(price, 4),
        "bid": q["bid"],
        "spread": round(spread, 4),
        "ask_size": q["ask_size"],
        "volume_24h": vol24,
        "close_ts": close_ts,
        "hours_to_close": round(hours, 2),
        "all_in_cost": round(all_in, 4),
        "score": round(score, 4),
    }, None


def find_candidates(markets, now_ts):
    candidates, rejections = [], Counter()
    for m in markets:
        cand, reason = evaluate(m, now_ts)
        if cand:
            candidates.append(cand)
        else:
            rejections[reason] += 1
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates, rejections
