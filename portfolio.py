"""
The imaginary account: cash, open bets, settled bets, sizing and safety brakes.

"Book equity" = cash + what we paid for open bets. It only moves when a bet
settles, so it equals starting bankroll + realized profit. The brakes use it.
"Mark equity" also values open bets at today's bid (what you could sell for).
"""
import csv
import json
import os
from datetime import datetime, timezone

import config
import strategy

ROUND = 4  # internal money precision; reports show cents


def utc_iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def new_state(now_ts):
    b = config.STARTING_BANKROLL
    return {
        "version": 1,
        "created_ts": now_ts,
        "starting_bankroll": b,
        "cash": b,
        "fees_paid": 0.0,
        "peak_equity": b,
        "day": utc_day(now_ts),
        "day_start_equity": b,
        "next_id": 1,
        "open_positions": [],
        "closed_positions": [],
        "equity_history": [],
        "last_run": {},
    }


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written file


def load_state(state_dir, now_ts):
    return load_json(os.path.join(state_dir, "portfolio.json"), None) or new_state(now_ts)


def save_state(state, state_dir):
    save_json(os.path.join(state_dir, "portfolio.json"), state)


LOG_FIELDS = ["time_utc", "action", "id", "ticker", "side", "contracts", "price", "fee",
              "cost", "payout", "pnl", "cash_after", "market"]


def append_log(log_path, row):
    new = not os.path.exists(log_path)
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LOG_FIELDS})


# ---------------------------------------------------------------------------
# Account math
# ---------------------------------------------------------------------------
def open_cost(state):
    return round(sum(p["cost"] for p in state["open_positions"]), ROUND)


def book_equity(state):
    return round(state["cash"] + open_cost(state), ROUND)


def mark_equity(state):
    marked = sum(p["contracts"] * p.get("mark_price", p["price"]) for p in state["open_positions"])
    return round(state["cash"] + marked, ROUND)


def roll_day(state, now_ts):
    day = utc_day(now_ts)
    if day != state["day"]:
        state["day"] = day
        state["day_start_equity"] = book_equity(state)


def update_peak(state):
    state["peak_equity"] = max(state["peak_equity"], book_equity(state))


def risk_halt_reason(state):
    eq = book_equity(state)
    if eq <= state["day_start_equity"] * (1 - config.DAILY_STOP_LOSS):
        return (f"Daily stop-loss: down {1 - eq / state['day_start_equity']:.1%} today. "
                "No new bets until tomorrow (UTC).")
    if eq <= state["peak_equity"] * (1 - config.MAX_DRAWDOWN_HALT):
        return (f"Drawdown brake: {1 - eq / state['peak_equity']:.1%} below peak. "
                "New bets paused — review settings before resetting.")
    return None


def update_phase(state, now_ts):
    """Start a new experiment phase whenever config.PHASE_NAME changes."""
    name = getattr(config, "PHASE_NAME", "")
    if not name or state.get("phase", {}).get("name") == name:
        return
    if state.get("phase"):
        state.setdefault("past_phases", []).append(dict(state["phase"], end_ts=round(now_ts)))
    state["phase"] = {"name": name, "start_ts": round(now_ts),
                      "start_equity": mark_equity(state), "start_book": book_equity(state)}
    # fresh brakes for the new experiment
    state["peak_equity"] = book_equity(state)
    state["day_start_equity"] = book_equity(state)


def phase_stats(state):
    ph = state.get("phase")
    if not ph:
        return None
    closed = [p for p in state["closed_positions"] if p.get("opened_ts", 0) >= ph["start_ts"]]
    wins = sum(1 for p in closed if p["pnl"] > 0)
    return {"name": ph["name"], "start_ts": ph["start_ts"],
            "pnl": mark_equity(state) - ph["start_equity"],
            "start_equity": ph["start_equity"], "wins": wins, "losses": len(closed) - wins}


def record_equity(state, now_ts):
    state["equity_history"].append([round(now_ts), book_equity(state), mark_equity(state)])
    state["equity_history"] = state["equity_history"][-5000:]


# ---------------------------------------------------------------------------
# Sizing and placing paper trades
# ---------------------------------------------------------------------------
def size_order(budget, price, max_contracts):
    """Most whole contracts we can buy (price + fee) within budget."""
    if budget <= 0:
        return 0
    per = price + strategy.fee_per_contract(price)
    n = int(min(budget / per, max_contracts))
    while n > 0 and n * price + strategy.taker_fee(n, price) > budget + 1e-9:
        n -= 1
    return max(n, 0)


def open_position(state, cand, contracts, now_ts, log_path):
    fee = strategy.taker_fee(contracts, cand["price"])
    cost = round(contracts * cand["price"] + fee, ROUND)
    pos = {
        "id": state["next_id"],
        "ticker": cand["ticker"],
        "event_ticker": cand["event_ticker"],
        "series": cand["series"],
        "label": cand["label"],
        "side": cand["side"],
        "contracts": contracts,
        "price": cand["price"],
        "fee": fee,
        "cost": cost,
        "opened_ts": round(now_ts),
        "close_ts": cand["close_ts"],
        "score": cand["score"],
        "mark_price": cand["bid"] or cand["price"],
        "shadow": {},  # shadow stop-loss triggers get recorded here
    }
    state["next_id"] += 1
    state["cash"] = round(state["cash"] - cost, ROUND)
    state["fees_paid"] = round(state["fees_paid"] + fee, ROUND)
    state["open_positions"].append(pos)
    append_log(log_path, {
        "time_utc": utc_iso(now_ts), "action": "OPEN", "id": pos["id"], "ticker": pos["ticker"],
        "side": pos["side"].upper(), "contracts": contracts, "price": pos["price"], "fee": fee,
        "cost": cost, "cash_after": state["cash"], "market": pos["label"],
    })
    return pos


def close_position(state, pos, payout_per_contract, result, now_ts, log_path):
    payout = round(pos["contracts"] * payout_per_contract, ROUND)
    pnl = round(payout - pos["cost"], ROUND)
    state["cash"] = round(state["cash"] + payout, ROUND)
    state["open_positions"] = [p for p in state["open_positions"] if p["id"] != pos["id"]]
    closed = dict(pos, payout=payout, pnl=pnl, result=result, closed_ts=round(now_ts),
                  won=payout_per_contract >= 0.5)
    closed.pop("mark_price", None)
    state["closed_positions"].append(closed)
    append_log(log_path, {
        "time_utc": utc_iso(now_ts), "action": "SETTLE", "id": pos["id"], "ticker": pos["ticker"],
        "side": pos["side"].upper(), "contracts": pos["contracts"], "price": pos["price"],
        "cost": pos["cost"], "payout": payout, "pnl": pnl, "cash_after": state["cash"],
        "market": pos["label"],
    })
    return closed


def shadow_key(drop):
    return str(int(round(drop * 100)))


def check_shadow_stops(pos, bid, now_ts):
    """Log (never execute) the first moment each stop-loss threshold would fire."""
    if "shadow" not in pos or bid is None or now_ts >= pos["close_ts"]:
        return  # untracked bet, no buyers, or trading already closed (can't sell)
    for drop in config.SHADOW_STOP_DROPS:
        key = shadow_key(drop)
        if key in pos["shadow"]:
            continue
        if bid <= pos["price"] - drop + 1e-9:
            fee = strategy.taker_fee(pos["contracts"], bid)
            pos["shadow"][key] = {
                "ts": round(now_ts),
                "bid": bid,
                "proceeds": round(pos["contracts"] * bid - fee, ROUND),
            }


def place_trades(state, candidates, now_ts, log_path, skips=None):
    """Walk the ranked candidates and open the ones that fit every risk rule."""
    equity = book_equity(state)
    exposure = open_cost(state)
    held_tickers = {p["ticker"] for p in state["open_positions"]}
    held_events = {p["event_ticker"] for p in state["open_positions"]}
    per_series, per_group = {}, {}
    for p in state["open_positions"]:
        per_series[p["series"]] = per_series.get(p["series"], 0) + 1
        g = strategy.related_group(p["label"])
        if g:
            per_group[g] = per_group.get(g, 0) + 1

    def skip(reason):
        if skips is not None:
            skips[reason] = skips.get(reason, 0) + 1

    opened = []
    for c in candidates:
        if len(opened) >= config.MAX_NEW_TRADES_PER_RUN:
            break
        if len(state["open_positions"]) >= config.MAX_OPEN_POSITIONS:
            break
        if c["ticker"] in held_tickers or c["event_ticker"] in held_events:
            continue  # one bet per event: sibling markets move together
        if c["price"] < getattr(config, "BET_MIN_PRICE", config.MIN_PRICE) - 1e-9:
            skip("tracked only (below bet price floor)")
            continue
        if per_series.get(c["series"], 0) >= config.MAX_POSITIONS_PER_SERIES:
            skip("series cap")
            continue
        group = strategy.related_group(c["label"])
        if group and per_group.get(group, 0) >= config.MAX_POSITIONS_PER_GROUP:
            skip(f"related-market cap ({group})")
            continue
        budget = min(
            equity * config.RISK_PER_TRADE,
            equity * config.MAX_TOTAL_EXPOSURE - exposure,
            state["cash"],
        )
        contracts = size_order(budget, c["price"], int(c["ask_size"]))
        if contracts < 1:
            continue
        pos = open_position(state, c, contracts, now_ts, log_path)
        opened.append(pos)
        exposure += pos["cost"]
        held_tickers.add(c["ticker"])
        held_events.add(c["event_ticker"])
        per_series[c["series"]] = per_series.get(c["series"], 0) + 1
        if group:
            per_group[group] = per_group.get(group, 0) + 1
    return opened
