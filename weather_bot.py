"""
Weather bot: a separate $100 paper account that trades Kalshi daily high/low
temperature markets with RESTING LIMIT ORDERS only (maker orders).

One run:
  1. Check which of our resting orders got filled by real public trades.
  2. Settle positions whose markets Kalshi has decided; cancel orders on closed markets.
  3. Build a forecast probability for every bracket (weather_model + weather_data).
  4. Decide the orders we want (3 strategies), then cancel / reprice / place to match.
  5. Score the model against the market, save state, rebuild the dashboard.

It's paper only: nothing is ever sent to Kalshi except public read requests.
"""
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import portfolio as pf          # reuse load/save helpers only
import strategy as fav          # reuse quote/settlement/timestamp helpers
import weather_config as wc
import weather_data as wd
import weather_model as wm

ROUND = 4
STRATEGIES = ("longshot", "favorite", "edge")
STRATEGY_NAMES = {"longshot": "Longshot NO", "favorite": "Favorite maker", "edge": "Model edge"}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def new_state(now_ts):
    b = wc.STARTING_BANKROLL
    return {"version": 1, "created_ts": round(now_ts), "starting_bankroll": b, "cash": b,
            "fees_paid": 0.0, "next_id": 1, "orders": [], "positions": [], "closed": [],
            "equity_history": [], "forecast_log": {}, "scorecard": {"pending": {}, "totals": {}},
            "last_run": {}}


def reserved(state):
    return round(sum(o["reserved"] for o in state["orders"]), ROUND)


def book_equity(state):
    return round(state["cash"] + reserved(state) + sum(p["cost"] for p in state["positions"]), ROUND)


def mark_equity(state):
    return round(state["cash"] + reserved(state) +
                 sum(p["contracts"] * p.get("mark_price", p["avg_price"]) for p in state["positions"]), ROUND)


def maker_fee(series, n, price):
    if not any(series.startswith(p) for p in wc.MAKER_FEE_SERIES_PREFIXES):
        return 0.0
    return math.ceil(wc.MAKER_FEE_RATE * n * price * (1 - price) * 100 - 1e-9) / 100


def update_phase(state, now_ts):
    if state.get("phase", {}).get("name") == wc.PHASE_NAME:
        return
    if state.get("phase"):
        state.setdefault("past_phases", []).append(dict(state["phase"], end_ts=round(now_ts)))
    state["phase"] = {"name": wc.PHASE_NAME, "start_ts": round(now_ts), "start_equity": book_equity(state)}
    state.pop("goal_reached_ts", None)


# ---------------------------------------------------------------------------
# Market helpers
# ---------------------------------------------------------------------------
def market_kind(m):
    t = m.get("ticker", "")
    for prefix, kind in wc.SERIES_KINDS.items():
        if t.startswith(prefix):
            return kind
    return None


def station_code(m):
    found = re.search(r"\(CLI([A-Z]{3})\)", (m.get("rules_primary") or "") + " " + (m.get("rules_secondary") or ""))
    return found.group(1) if found else None


def is_weather_market(m):
    return (market_kind(m) is not None and (m.get("market_type") or "binary") == "binary"
            and station_code(m) is not None and wm.bracket_bounds(m) is not None
            and wm.event_date(m.get("event_ticker", "")) is not None)


# ---------------------------------------------------------------------------
# Orders, fills, positions
# ---------------------------------------------------------------------------
def place_order(state, spec, contracts, now_ts):
    fee = maker_fee(spec["series"], contracts, spec["price"])
    cost = round(contracts * spec["price"] + fee, ROUND)
    o = dict(spec, id=state["next_id"], contracts=contracts, filled=0.0, placed_ts=round(now_ts),
             checked_ts=round(now_ts), reserved=cost)
    state["next_id"] += 1
    state["cash"] = round(state["cash"] - cost, ROUND)
    state["orders"].append(o)
    return o


def cancel_order(state, o, reason, run):
    state["cash"] = round(state["cash"] + o["reserved"], ROUND)
    state["orders"] = [x for x in state["orders"] if x["id"] != o["id"]]
    run["cancelled"].append({"ticker": o["ticker"], "reason": reason})


def apply_fill(state, o, qty, now_ts):
    qty = round(min(qty, o["contracts"] - o["filled"]), 2)
    if qty <= 0:
        return 0
    fee = maker_fee(o["series"], qty, o["price"])
    cost = round(qty * o["price"] + fee, ROUND)
    o["filled"] = round(o["filled"] + qty, 2)
    remaining = round(o["contracts"] - o["filled"], 2)
    new_reserve = round(remaining * o["price"] + maker_fee(o["series"], remaining, o["price"]), ROUND)
    state["cash"] = round(state["cash"] + o["reserved"] - new_reserve - cost, ROUND)
    o["reserved"] = new_reserve
    state["fees_paid"] = round(state["fees_paid"] + fee, ROUND)
    pos = next((p for p in state["positions"] if p["ticker"] == o["ticker"] and p["side"] == o["side"]), None)
    if pos is None:
        pos = {k: o[k] for k in ("ticker", "event_ticker", "series", "kind", "code", "date", "label",
                                 "side", "strategy", "close_ts", "q_model")}
        pos.update(id=o["id"], contracts=0.0, cost=0.0, opened_ts=round(now_ts), mark_price=o["price"])
        state["positions"].append(pos)
    pos["contracts"] = round(pos["contracts"] + qty, 2)
    pos["cost"] = round(pos["cost"] + cost, ROUND)
    pos["avg_price"] = round(pos["cost"] / pos["contracts"], ROUND)
    return qty


def trade_fill_qty(o, t):
    """How much of a public trade would have filled our resting order (0 if none)."""
    taker = t.get("taker_outcome_side") or t.get("taker_side")
    try:
        count = float(t.get("count_fp") or t.get("count") or 0)
        yes_p = float(t.get("yes_price_dollars")) if t.get("yes_price_dollars") is not None else None
        no_p = float(t.get("no_price_dollars")) if t.get("no_price_dollars") is not None else None
    except (TypeError, ValueError):
        return 0.0
    if o["side"] == "no":
        # someone BOUGHT YES; the resting sellers of YES are NO bidders like us
        px = no_p if no_p is not None else (1 - yes_p if yes_p is not None else None)
        hit = taker == "yes"
    else:
        px = yes_p if yes_p is not None else (1 - no_p if no_p is not None else None)
        hit = taker == "no"
    if not hit or px is None or px > o["price"] + 1e-9:
        return 0.0
    return count if px < o["price"] - 1e-9 else count * wc.FILL_AT_PRICE_SHARE


def check_fills(api, state, now_ts, run):
    for o in list(state["orders"]):
        try:
            trades = api.trades(o["ticker"], min_ts=o["checked_ts"])
        except Exception as e:  # noqa: BLE001
            run["errors"].append(f"trades {o['ticker']}: {e}")
            continue
        for t in sorted(trades, key=lambda t: t.get("created_time", "")):
            ts = fav.parse_ts(t.get("created_time"))
            if ts is None or ts < o["placed_ts"]:
                continue
            q = trade_fill_qty(o, t)
            if q > 0:
                got = apply_fill(state, o, q, now_ts)
                if got:
                    run["fills"].append({"ticker": o["ticker"], "side": o["side"], "qty": got,
                                         "price": o["price"], "strategy": o["strategy"]})
            if o["filled"] >= o["contracts"] - 1e-9:
                break
        o["checked_ts"] = round(now_ts)
        if o["filled"] >= o["contracts"] - 1e-9:
            state["orders"] = [x for x in state["orders"] if x["id"] != o["id"]]


def settle_and_cleanup(state, markets, now_ts, run):
    for p in list(state["positions"]):
        m = markets.get(p["ticker"])
        if not m:
            continue
        bid = fav.side_quote(m, p["side"])["bid"]
        if bid is not None and m.get("status") in ("active", "open"):
            p["mark_price"] = bid
        payout_pc = fav.settlement_payout(m, p["side"])
        if payout_pc is None:
            continue
        payout = round(p["contracts"] * payout_pc, ROUND)
        pnl = round(payout - p["cost"], ROUND)
        state["cash"] = round(state["cash"] + payout, ROUND)
        state["positions"] = [x for x in state["positions"] if x is not p]
        rec = dict(p, payout=payout, pnl=pnl, result=(m.get("result") or "").upper(),
                   closed_ts=round(now_ts), won=payout_pc >= 0.5)
        rec.pop("mark_price", None)
        state["closed"].append(rec)
        run["settled"].append({"ticker": p["ticker"], "pnl": pnl})
    for o in list(state["orders"]):
        m = markets.get(o["ticker"])
        closing = (o["close_ts"] - now_ts) < wc.STOP_POSTING_MINUTES_BEFORE_CLOSE * 60
        if not m or m.get("status") not in ("active", "open") or closing:
            cancel_order(state, o, "market closing", run)


# ---------------------------------------------------------------------------
# Model per event
# ---------------------------------------------------------------------------
def own_calibration(state, code, kind):
    pairs = [(v["fc"], v["actual"]) for k, v in state["forecast_log"].items()
             if k.startswith(f"{code}|") and k.endswith(f"|{kind}") and "actual" in v]
    pairs = pairs[-21:]
    cal = wm.calibrate(pairs)
    if cal:
        variances = [v.get("var", 0) for k, v in state["forecast_log"].items()
                     if k.startswith(f"{code}|") and k.endswith(f"|{kind}") and "actual" in v][-21:]
        cal["ens_var"] = sum(variances) / len(variances) if variances else 0
    return cal


def model_event(state, data, ev, now_ts):
    """Returns a dict with components and per-market probabilities, or None."""
    code, date, kind = ev["code"], ev["date"], ev["kind"]
    st = data.station(code)
    if not st:
        return None
    ext = (data.forecast_extremes(code, st).get(date) or {}).get(kind)
    if not ext:
        return None
    nbm = (data.nbm_extremes(code, st).get(date) or {}).get(kind)
    start, end = wm.climate_day_window(date, st["std_offset"])

    # bias + spread: our own track record first, then 30-day history, then defaults
    own = own_calibration(state, code, kind)
    hist = (data.calibration(code, st) or {}).get(kind)
    if own:
        bias, src = own["bias"], f"own {own['n']}d"
        ksd = math.sqrt(max(own["sd"] ** 2 - own.get("ens_var", 0), wc.MIN_KERNEL_SD_F ** 2))
        ksd = min(ksd, 3.0)
    elif hist:
        bias, src, ksd = hist["bias"], f"history {hist['n']}d", wc.KERNEL_SD_F
    else:
        bias, src, ksd = wc.DEFAULT_BIAS_F[kind], "default", wc.KERNEL_SD_F
    comps = wm.build_components(ext, bias, ksd, nbm)
    if not comps:
        return None

    observed = None
    if start <= now_ts < end:
        hi, lo = data.observed_so_far(code, start)
        observed = hi if kind == "high" else lo

    # log the raw ensemble forecast made before the day starts, for our own calibration
    raw = [v for vals in ext.values() for v in vals]
    key = f"{code}|{date}|{kind}"
    if now_ts < start and raw:
        mean = sum(raw) / len(raw)
        var = sum((v - mean) ** 2 for v in raw) / len(raw)
        state["forecast_log"][key] = {"fc": round(mean, 2), "var": round(var, 2), "ts": round(now_ts)}
    return {"station": st, "comps": comps, "observed": observed, "bias": bias, "bias_src": src,
            "kernel_sd": round(ksd, 2), "nbm": nbm, "start": start, "end": end,
            "summary": wm.summary(comps), "members": {k: len(v) for k, v in ext.items()}}


def fill_actuals(state, data, codes, now_ts):
    """Record official highs/lows for past forecast-log days (for calibration)."""
    today = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%d")
    for code in codes:
        need = [k for k, v in state["forecast_log"].items()
                if k.startswith(code + "|") and "actual" not in v and k.split("|")[1] < today]
        if not need:
            continue
        years = {k.split("|")[1][:4] for k in need}
        actual = {}
        for y in years:
            actual.update(data.climate_actuals(code, int(y)))
        for k in need:
            _, d, kind = k.split("|")
            v = (actual.get(d) or {}).get(kind)
            if v is not None:
                state["forecast_log"][k]["actual"] = v
    cutoff = (datetime.fromtimestamp(now_ts, timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    state["forecast_log"] = {k: v for k, v in state["forecast_log"].items() if k.split("|")[1] >= cutoff}


# ---------------------------------------------------------------------------
# Strategies -> desired orders
# ---------------------------------------------------------------------------
def post_price(bid, ask, limit):
    """Maker price: improve the best bid by 1c if there's room, never cross the ask.
    Returns None if our limit is below the current best bid: a 'lowball' order behind the
    book only fills when the price crashes, i.e. right when news has turned against us."""
    b = bid or 0.0
    a = ask if ask is not None else 1.0
    target = b + 0.01 if a - b > 0.0101 else b
    if target <= 0:
        target = 0.01
    price = min(target, limit, a - 0.01)
    price = math.floor(price * 100 + 1e-6) / 100
    if price < b - 1e-9:
        return None
    return price if 0.01 <= price <= 0.99 else None


def desired_orders(m, mod, now_ts):
    """Candidate orders for one market. Returns list of specs (best first)."""
    lo, hi = wm.bracket_bounds(m)
    kind = market_kind(m)
    q_yes = wm.prob_in(mod["comps"], lo, hi, kind, mod["observed"])
    if q_yes is None:
        return [], None
    q_models = {k: wm.prob_in(mod["comps"], lo, hi, kind, mod["observed"], models={k})
                for k in ("ecmwf", "gfs")}
    close_ts = fav.parse_ts(m.get("close_time")) or 0
    if (close_ts - now_ts) < wc.STOP_POSTING_MINUTES_BEFORE_CLOSE * 60:
        return [], q_yes
    st = mod["station"]
    local = wm.local_clock(now_ts, st)
    ev_date = datetime.strptime(wm.event_date(m["event_ticker"]), "%Y-%m-%d").date()
    day_before = local.date() == ev_date - timedelta(days=1)
    lead_hours = (mod["start"] - now_ts) / 3600
    yes_bid = fav.side_quote(m, "yes")["bid"] or 0.0

    out = []
    for side in ("yes", "no"):
        q = q_yes if side == "yes" else 1 - q_yes
        qm = {k: (v if side == "yes" else 1 - v) for k, v in q_models.items() if v is not None}
        quote = fav.side_quote(m, side)
        bid, ask = quote["bid"], quote["ask"]
        base = {"ticker": m["ticker"], "event_ticker": m["event_ticker"], "series": fav.series_of(m),
                "kind": kind, "code": st["code"], "date": wm.event_date(m["event_ticker"]),
                "label": f"{st['code']} {kind} {wm.bracket_label(lo, hi)} {wm.event_date(m['event_ticker'])[5:]}",
                "side": side, "close_ts": close_ts, "q_model": round(q, 4),
                "mid": round(((bid or 0) + (ask if ask is not None else 1)) / 2, 4)}

        L = wc.LONGSHOT
        if (L["enabled"] and side == "no" and day_before and L["local_hours"][0] <= local.hour < L["local_hours"][1]
                and L["yes_bid_range"][0] <= yes_bid <= L["yes_bid_range"][1] and q_yes <= L["max_model_prob"]):
            p = post_price(bid, ask, L["max_no_price"])
            if p:
                out.append(dict(base, strategy="longshot", price=p, edge=round(q - p, 4)))
                continue

        F = wc.FAVORITE
        if (F["enabled"] and bid is not None and F["price_range"][0] <= bid <= F["price_range"][1]
                and q >= F["min_model_prob"]):
            p = post_price(bid, ask, min(F["price_range"][1], q - F["margin"]))
            if p and p >= F["price_range"][0]:
                out.append(dict(base, strategy="favorite", price=p, edge=round(q - p, 4)))
                continue

        E = wc.EDGE
        if E["enabled"] and lead_hours <= E["max_lead_hours"] and len(qm) == 2:
            p = post_price(bid, ask, q - E["min_edge"])
            if (p and E["price_range"][0] <= p <= E["price_range"][1] and q - p >= E["min_edge"] - 1e-9
                    and all(v - p >= E["min_each_model_edge"] for v in qm.values())):
                out.append(dict(base, strategy="edge", price=p, edge=round(q - p, 4)))
    out.sort(key=lambda s: -s["edge"])
    return out[:1], q_yes


def size_order(state, spec, eq):
    q_used = wc.MODEL_WEIGHT_FOR_SIZING * spec["q_model"] + (1 - wc.MODEL_WEIGHT_FOR_SIZING) * spec["mid"]
    f = min(wc.KELLY_FRACTION * wm.kelly(q_used, spec["price"]), wc.MAX_BET_FRACTION)
    if f <= 0:
        return 0, round(q_used, 4)
    per = spec["price"] + maker_fee(spec["series"], 1, spec["price"])
    used_event = sum(o["reserved"] for o in state["orders"] if o["event_ticker"] == spec["event_ticker"]) + \
        sum(p["cost"] for p in state["positions"] if p["event_ticker"] == spec["event_ticker"])
    used_total = eq - state["cash"]
    budget = min(f * eq, wc.MAX_EVENT_FRACTION * eq - used_event,
                 wc.MAX_TOTAL_EXPOSURE * eq - used_total, state["cash"])
    n = math.floor(max(budget, 0) / per)
    return n, round(q_used, 4)


# ---------------------------------------------------------------------------
# Scorecard: is the model more accurate than the market price?
# ---------------------------------------------------------------------------
def update_scorecard(state, api, snapshots, now_ts, run):
    sc = state["scorecard"]
    pend = sc["pending"]
    for s in snapshots:          # keep the latest day-before estimate for every bracket
        if now_ts < s["start"]:
            pend[s["ticker"]] = {"q": s["q"], "mkt": s["mkt"], "kind": s["kind"], "close_ts": s["close_ts"]}
    due = [t for t, v in pend.items() if v["close_ts"] < now_ts][:300]
    if not due:
        return
    try:
        res = api.markets_by_ticker(due)
    except Exception as e:  # noqa: BLE001
        run["errors"].append(f"scorecard: {e}")
        return
    for t in due:
        m = res.get(t)
        if not m:
            if now_ts - pend[t]["close_ts"] > 7 * 86400:
                pend.pop(t)
            continue
        y = fav.settlement_payout(m, "yes")
        if y is None:
            continue
        v = pend.pop(t)
        for key in ("all", v["kind"]):
            tot = sc["totals"].setdefault(key, {"n": 0, "model": 0.0, "market": 0.0})
            tot["n"] += 1
            tot["model"] += (v["q"] - y) ** 2
            tot["market"] += (v["mkt"] - y) ** 2


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------
def run_cycle(api, data=None, now_ts=None, state_dir=wc.STATE_DIR, verbose=True):
    now_ts = now_ts or time.time()
    os.makedirs(state_dir, exist_ok=True)
    sp, cp = os.path.join(state_dir, "portfolio.json"), os.path.join(state_dir, "cache.json")
    state = pf.load_json(sp, None) or new_state(now_ts)
    cache = pf.load_json(cp, {})
    data = data or wd.WeatherData(cache, now_ts)
    update_phase(state, now_ts)
    run = {"ts": round(now_ts), "error": None, "errors": [], "fills": [], "settled": [], "cancelled": [],
           "placed": [], "markets": 0, "events": [], "halt": None, "candidates": 0}
    t0 = time.time()
    try:
        markets = api.markets_closing_between(now_ts + 60, now_ts + wc.MAX_HOURS_AHEAD * 3600)
        wx = {m["ticker"]: m for m in markets if is_weather_market(m)}
        run["markets"] = len(wx)

        check_fills(api, state, now_ts, run)
        known = dict(wx)
        missing = {x["ticker"] for x in state["orders"] + state["positions"]} - set(known)
        if missing:
            known.update(api.markets_by_ticker(sorted(missing)))
        settle_and_cleanup(state, known, now_ts, run)

        # group into events (one city, one day, high or low)
        events = {}
        for m in wx.values():
            ev = events.setdefault(m["event_ticker"], {"code": station_code(m), "kind": market_kind(m),
                                                       "date": wm.event_date(m["event_ticker"]), "markets": []})
            ev["markets"].append(m)

        # models: fetch per station in parallel (network-bound)
        lock = threading.Lock()
        models = {}

        def work(et):
            if time.time() - t0 > wc.TIME_BUDGET_SECONDS:
                return
            mod = model_event(state, data, events[et], now_ts)
            with lock:
                models[et] = mod
        stations = {}
        for et, ev in events.items():
            stations.setdefault(ev["code"], []).append(et)
        with ThreadPoolExecutor(max_workers=6) as ex:
            # one worker per station keeps each station's cache fetches serialized
            list(ex.map(lambda ets: [work(e) for e in ets], stations.values()))
        fill_actuals(state, data, list(stations), now_ts)

        # desired orders
        eq = book_equity(state)
        desired, snapshots = {}, []
        for et, ev in events.items():
            mod = models.get(et)
            if not mod:
                continue
            rows = []
            for m in ev["markets"]:
                specs, q_yes = desired_orders(m, mod, now_ts)
                if q_yes is None:
                    continue
                yq = fav.side_quote(m, "yes")
                mid = ((yq["bid"] or 0) + (yq["ask"] if yq["ask"] is not None else 1)) / 2
                lo, hi = wm.bracket_bounds(m)
                rows.append({"label": wm.bracket_label(lo, hi), "lo": lo, "q": round(q_yes, 3),
                             "bid": yq["bid"], "ask": yq["ask"]})
                snapshots.append({"ticker": m["ticker"], "q": q_yes, "mkt": mid, "kind": ev["kind"],
                                  "start": mod["start"], "close_ts": fav.parse_ts(m.get("close_time")) or 0})
                for s in specs:
                    desired[s["ticker"]] = s
            rows.sort(key=lambda r: r["lo"])
            run["events"].append({"event": et, "code": ev["code"], "kind": ev["kind"], "date": ev["date"],
                                  "summary": mod["summary"], "nbm": mod["nbm"], "observed": mod["observed"],
                                  "bias": mod["bias"], "bias_src": mod["bias_src"],
                                  "kernel_sd": mod["kernel_sd"], "members": mod["members"],
                                  "name": mod["station"].get("name"),
                                  "rows": [{k: r[k] for k in ("label", "q", "bid", "ask")} for r in rows]})
        run["candidates"] = len(desired)

        # brakes
        eq = book_equity(state)
        if eq >= wc.GOAL_EQUITY:
            state.setdefault("goal_reached_ts", round(now_ts))
        if state.get("goal_reached_ts"):
            run["halt"] = f"Goal reached (${wc.GOAL_EQUITY:.0f}). Locked in: no new bets."
        elif eq <= wc.FLOOR_EQUITY:
            run["halt"] = f"Account at or below ${wc.FLOOR_EQUITY:.0f} floor: no new bets."
        if run["halt"]:
            desired = {}

        # reconcile resting orders with what we want now
        for o in list(state["orders"]):
            spec = desired.get(o["ticker"])
            if not spec or spec["side"] != o["side"]:
                cancel_order(state, o, "no longer qualifies", run)
            elif abs(spec["price"] - o["price"]) > 0.004:
                cancel_order(state, o, f"reprice {o['price']:.2f}->{spec['price']:.2f}", run)
            else:
                o["q_model"] = spec["q_model"]
                desired.pop(o["ticker"])        # keep it resting (keeps queue position)
        held = {p["ticker"] for p in state["positions"]}
        eq = book_equity(state)
        # research-backed strategies get first call on the budget, experimental last
        for spec in sorted(desired.values(), key=lambda s: (STRATEGIES.index(s["strategy"]), -s["edge"])):
            if spec["ticker"] in held or len(state["orders"]) >= wc.MAX_OPEN_ORDERS:
                continue
            n, q_used = size_order(state, spec, eq)
            if n < 1:
                continue
            o = place_order(state, dict(spec, q_used=q_used), n, now_ts)
            run["placed"].append({"ticker": o["ticker"], "side": o["side"], "price": o["price"],
                                  "contracts": n, "strategy": o["strategy"]})

        update_scorecard(state, api, snapshots, now_ts, run)
    except Exception as e:  # noqa: BLE001
        run["error"] = f"{type(e).__name__}: {e}"

    run["health"] = getattr(data, "health", {})
    run["seconds"] = round(time.time() - t0, 1)
    state["equity_history"].append([round(now_ts), book_equity(state), mark_equity(state)])
    state["equity_history"] = state["equity_history"][-5000:]
    state["closed"] = state["closed"][-2000:]
    state["last_run"] = run
    pf.save_json(sp, state)
    pf.save_json(cp, cache)
    if verbose:
        print(f"[weather] {run['markets']} markets, {len(run['events'])} events modeled, "
              f"{len(run['fills'])} fills, {len(run['placed'])} new orders, {len(state['orders'])} resting, "
              f"{len(state['positions'])} positions, equity ${mark_equity(state):.2f} ({run['seconds']}s)")
        if run["halt"]:
            print("  " + run["halt"])
        if run["error"]:
            print("  ERROR: " + run["error"])
    return run["error"] is None
