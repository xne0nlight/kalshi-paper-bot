"""
Sports parlay bot (paper money): NFL, college football, NBA, NHL game winners.

Every parlay is placed in two $100 accounts at once:

  ROLLING  Leg 1 is bought as a normal single-game contract with a resting limit order.
           If it wins, the whole payout rides on leg 2, and so on. Same payout shape as
           a parlay, but each leg costs a cent or two instead of the combo cut.
           If a leg can't be bought at a good price before its game starts, the chain
           stops and keeps what it has won so far ("banked").
  COMBO    The same legs bought as one Kalshi combo, priced with the ~15% cut combo
           bettors have historically paid. Locked in up front: all legs must win.

Legs are chosen where the sportsbooks' consensus (margin removed, sharp books weighted
up) says a team is more likely to win than Kalshi's price implies.
"""
import math
import os
import time
from datetime import datetime, timedelta, timezone

import portfolio as pf
import sports_config as sc
import sports_data as sd
import strategy as fav
import weather_bot as wb      # reuse maker pricing + trade-print fill rules

ROUND = 4


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def new_state(now_ts):
    acct = lambda: {"start": sc.STARTING_BANKROLL, "cash": sc.STARTING_BANKROLL, "history": []}  # noqa: E731
    return {"version": 1, "created_ts": round(now_ts), "rolling": acct(), "combo": acct(),
            "parlays": [], "next_id": 1, "scorecard": {"pending": {}, "totals": {}}, "last_run": {}}


def maker_fee(n, p):
    return math.ceil(sc.MAKER_FEE_RATE * n * p * (1 - p) * 100 - 1e-9) / 100


def taker_fee(n, p):
    return math.ceil(sc.TAKER_FEE_RATE * n * p * (1 - p) * 100 - 1e-9) / 100


def leg_value(leg, mark=False):
    v = leg["cost"]
    if mark and leg["contracts"] and leg.get("mark") is not None:
        v = leg["contracts"] * leg["mark"]
    if leg.get("order"):
        v += leg["order"]["reserved"]
    return v


def rolling_equity(state, mark=False):
    eq = state["rolling"]["cash"]
    for p in state["parlays"]:
        if p["status"] == "active":
            eq += p["chain_cash"] + sum(leg_value(l, mark) for l in p["legs"] if l["status"] in ("ordered", "filled"))
    return round(eq, ROUND)


def combo_equity(state):
    return round(state["combo"]["cash"] + sum(p["combo"]["stake"] for p in state["parlays"]
                                              if p["combo"]["status"] == "active"), ROUND)


# ---------------------------------------------------------------------------
# Games from Kalshi + odds
# ---------------------------------------------------------------------------
def kalshi_games(markets, league, now_ts):
    """Group team-winner markets into games: {event_ticker: {...}}."""
    games = {}
    for m in markets:
        team = m.get("yes_sub_title") or (m.get("title") or "").replace(" wins", "")
        if not team or (m.get("market_type") or "binary") != "binary":
            continue
        close = fav.parse_ts(m.get("close_time"))
        exp = fav.parse_ts(m.get("expected_expiration_time"))
        start = close - sc.START_FROM_CLOSE_HOURS * 3600 if close else (exp - 3 * 3600 if exp else None)
        g = games.setdefault(m["event_ticker"], {"league": league, "event": m["event_ticker"],
                                                 "start_ts": start, "teams": {}})
        g["teams"][team] = m
    return {k: g for k, g in games.items() if len(g["teams"]) == 2 and g["start_ts"]}


def evaluate_game(g, odds_events, odds_ts, espn_games, now_ts):
    """Attach fair probabilities and per-team leg candidates. Returns list of board rows."""
    a, b = list(g["teams"])
    ev, names = sd.match_pair(a, b, g["start_ts"], odds_events)
    if ev and ev.get("start_ts"):
        g["start_ts"] = ev["start_ts"]               # sportsbooks have exact kickoff times
    eg, enames = sd.match_pair(a, b, g["start_ts"], espn_games)
    g["espn"] = None
    if eg:
        g["espn"] = {"state": eg.get("state"), "detail": eg.get("detail"),
                     "records": {t: next((v.get("record") for v in eg["teams"].values()
                                          if v.get("name") == enames[t]), None) for t in (a, b)}}
    rows = []
    for team, m in g["teams"].items():
        q = ev["fair"].get(names[team]) if ev else None
        quote = fav.side_quote(m, "yes")
        bid, ask = quote["bid"], quote["ask"]
        mid = round(((bid or 0) + (ask if ask is not None else 1)) / 2, 4)
        row = {"league": g["league"], "event": g["event"], "ticker": m["ticker"], "team": team,
               "opp": [t for t in g["teams"] if t != team][0], "start_ts": g["start_ts"],
               "bid": bid, "ask": ask, "mid": mid, "q": q, "books": ev["books"] if ev else 0,
               "sharp": ev.get("sharp") if ev else False, "odds_age_h": None, "price": None,
               "edge": None, "ok": False, "why": "",
               "record": (g["espn"] or {}).get("records", {}).get(team) if g.get("espn") else None,
               "state": (g["espn"] or {}).get("state") if g.get("espn") else None}
        if odds_ts:
            row["odds_age_h"] = round((now_ts - odds_ts) / 3600, 1)
        lo, hi = sc.LEG_PROB_RANGE
        if q is None:
            row["why"] = "no sportsbook match"
        elif row["state"] in ("in", "post") or g["start_ts"] < now_ts + 30 * 60:
            row["why"] = "started"
        elif g["start_ts"] > now_ts + sc.HORIZON_HOURS * 3600:
            row["why"] = "too far out"
        elif not (lo <= q <= hi):
            row["why"] = "probability out of range"
        elif bid is None or ask is None or ask - bid > sc.LEG_MAX_SPREAD + 1e-9:
            row["why"] = "spread too wide"
        elif abs(q - mid) > sc.LEG_MAX_DISAGREEMENT:
            row["why"] = "books & Kalshi disagree a lot (news?)"
        elif row["odds_age_h"] is None or row["odds_age_h"] > sc.ODDS_MAX_AGE_HOURS:
            row["why"] = "odds too old"
        else:
            p = wb.post_price(bid, ask, q - sc.LEG_MIN_EDGE)
            if p is None:
                row["why"] = "no edge vs Kalshi"
            else:
                row.update(price=p, edge=round(q - p, 4), ok=True, why="eligible")
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Building parlays
# ---------------------------------------------------------------------------
def q_used(leg):
    return sc.FAIR_WEIGHT_FOR_SIZING * leg["q"] + (1 - sc.FAIR_WEIGHT_FOR_SIZING) * leg["mid"]


def best_chain(cands, now_ts, used_events):
    """Highest expected-value chain of 2-4 legs whose games don't overlap in time."""
    legs = sorted([c for c in cands if c["ok"] and c["event"] not in used_events], key=lambda c: c["start_ts"])
    best, best_ev = None, 0.0

    def ends(c):
        return c["start_ts"] + (sc.LEAGUES[c["league"]]["hours"] + sc.SETTLE_BUFFER_HOURS) * 3600

    def dfs(chain, i):
        nonlocal best, best_ev
        if len(chain) >= sc.MIN_LEGS:
            price = math.prod(c["price"] for c in chain)
            if sc.PARLAY_PRICE_RANGE[0] <= price <= sc.PARLAY_PRICE_RANGE[1]:
                ev = math.prod(q_used(c) / c["price"] for c in chain) - 1
                if ev > best_ev:
                    best, best_ev = list(chain), ev
        if len(chain) == sc.MAX_LEGS:
            return
        for j in range(i, len(legs)):
            c = legs[j]
            if chain and c["start_ts"] < ends(chain[-1]):
                continue
            if any(c["event"] == x["event"] for x in chain):
                continue
            dfs(chain + [c], j + 1)

    for k, first in enumerate(legs):
        if first["start_ts"] - now_ts <= sc.FIRST_LEG_WITHIN_HOURS * 3600:
            dfs([first], k + 1)
    return best, best_ev


def create_parlay(state, chain, ev, now_ts, run):
    q_par = math.prod(q_used(c) for c in chain)
    p_par = math.prod(c["price"] for c in chain)
    f = min(sc.KELLY_FRACTION * wb.wm.kelly(q_par, p_par), sc.MAX_STAKE_FRACTION)
    r_eq, c_eq = rolling_equity(state), combo_equity(state)
    stake = round(min(f * r_eq, state["rolling"]["cash"]), 2)
    if stake < sc.MIN_STAKE:
        return None
    legs = [{"league": c["league"], "ticker": c["ticker"], "event": c["event"], "team": c["team"],
             "opp": c["opp"], "start_ts": c["start_ts"], "q": c["q"], "mid": c["mid"], "price_plan": c["price"],
             "status": "planned", "order": None, "contracts": 0.0, "cost": 0.0, "result": None, "mark": None}
            for c in chain]
    combo_price = min(0.99, math.prod(c["mid"] for c in chain) / (1 - sc.COMBO_HOLD))
    c_stake = round(min(f * c_eq, state["combo"]["cash"]), 2)
    combo = {"stake": c_stake, "price": round(combo_price, 4), "contracts": round(c_stake / combo_price, 2),
             "status": "active" if c_stake >= sc.MIN_STAKE else "skipped", "payout": None, "pnl": None}
    if combo["status"] == "active":
        state["combo"]["cash"] = round(state["combo"]["cash"] - c_stake, ROUND)
    else:
        combo["stake"] = 0.0
    p = {"id": state["next_id"], "created_ts": round(now_ts), "legs": legs, "stake": stake, "status": "active",
         "cur": 0, "chain_cash": stake, "payout": None, "pnl": None, "closed_ts": None,
         "q_par": round(q_par, 4), "p_par": round(p_par, 4), "ev": round(ev, 4), "combo": combo, "note": ""}
    state["next_id"] += 1
    state["rolling"]["cash"] = round(state["rolling"]["cash"] - stake, ROUND)
    state["parlays"].append(p)
    run["created"].append({"id": p["id"], "legs": [l["team"] for l in legs], "stake": stake,
                           "pays": round(1 / p_par, 2)})
    return p


# ---------------------------------------------------------------------------
# Rolling account: one parlay's state machine
# ---------------------------------------------------------------------------
def place_leg_order(p, leg, price, now_ts):
    n = math.floor(p["chain_cash"] / (price + maker_fee(1, price)) * 100) / 100
    while n > 0 and n * price + maker_fee(n, price) > p["chain_cash"] + 1e-9:
        n = round(n - 0.01, 2)
    if n < 1:
        return False
    reserve = round(n * price + maker_fee(n, price), ROUND)
    p["chain_cash"] = round(p["chain_cash"] - reserve, ROUND)
    leg["order"] = {"price": price, "contracts": n, "filled": 0.0, "placed_ts": round(now_ts),
                    "checked_ts": round(now_ts), "reserved": reserve}
    leg["status"] = "ordered"
    return True


def cancel_leg_order(p, leg):
    o = leg.get("order")
    if o:
        p["chain_cash"] = round(p["chain_cash"] + o["reserved"], ROUND)
        leg["order"] = None


def fill_leg(leg, qty, price, fee):
    leg["contracts"] = round(leg["contracts"] + qty, 2)
    leg["cost"] = round(leg["cost"] + qty * price + fee, ROUND)


def check_leg_fills(api, p, leg, now_ts, run):
    o = leg["order"]
    trades = api.trades(leg["ticker"], min_ts=o["checked_ts"])
    for t in sorted(trades, key=lambda t: t.get("created_time", "")):
        ts = fav.parse_ts(t.get("created_time"))
        if ts is None or ts < o["placed_ts"]:
            continue
        q = wb.trade_fill_qty({"side": "yes", "price": o["price"]}, t)
        q = round(min(q, o["contracts"] - o["filled"]), 2)
        if q <= 0:
            continue
        fee = maker_fee(q, o["price"])
        o["filled"] = round(o["filled"] + q, 2)
        remaining = round(o["contracts"] - o["filled"], 2)
        new_res = round(remaining * o["price"] + maker_fee(remaining, o["price"]), ROUND)
        p["chain_cash"] = round(p["chain_cash"] + o["reserved"] - new_res - q * o["price"] - fee, ROUND)
        o["reserved"] = new_res
        fill_leg(leg, q, o["price"], fee)
        run["fills"].append({"parlay": p["id"], "team": leg["team"], "qty": q, "price": o["price"]})
        if remaining <= 0:
            break
    o["checked_ts"] = round(now_ts)
    if o["filled"] >= o["contracts"] - 1e-9:
        p["chain_cash"] = round(p["chain_cash"] + o["reserved"], ROUND)
        leg["order"] = None
        leg["status"] = "filled"


def close_parlay(state, p, status, now_ts, note=""):
    p["status"] = status
    p["payout"] = round(p["chain_cash"], ROUND)
    p["pnl"] = round(p["payout"] - p["stake"], ROUND)
    p["closed_ts"] = round(now_ts)
    p["note"] = note
    state["rolling"]["cash"] = round(state["rolling"]["cash"] + p["chain_cash"], ROUND)
    p["chain_cash"] = 0.0


def step_rolling(api, state, p, rows_by_ticker, markets, now_ts, run):
    while p["status"] == "active":
        leg = p["legs"][p["cur"]]
        m = markets.get(leg["ticker"])
        cutoff = leg["start_ts"] - sc.LEG_CUTOFF_MINUTES * 60
        if m:
            bid = fav.side_quote(m, "yes")["bid"]
            if bid is not None and m.get("status") in ("active", "open"):
                leg["mark"] = bid

        if leg["status"] == "ordered":
            check_leg_fills(api, p, leg, now_ts, run)

        if leg["status"] in ("planned", "ordered") and now_ts >= cutoff:
            # game about to start: take the ask for any unfilled part if it's still +EV, else stop here
            row = rows_by_ticker.get(leg["ticker"])
            q_now = (row or {}).get("q") or leg["q"]
            ask = fav.side_quote(m, "yes")["ask"] if m else None
            cancel_leg_order(p, leg)
            if (ask is not None and now_ts < leg["start_ts"]
                    and q_now - ask - taker_fee(1, ask) >= sc.TAKER_MIN_EDGE):
                n = math.floor(p["chain_cash"] / (ask + taker_fee(1, ask)) * 100) / 100
                while n > 0 and n * ask + taker_fee(n, ask) > p["chain_cash"] + 1e-9:
                    n = round(n - 0.01, 2)
                if n >= 1:
                    fee = taker_fee(n, ask)
                    p["chain_cash"] = round(p["chain_cash"] - n * ask - fee, ROUND)
                    fill_leg(leg, n, ask, fee)
                    run["fills"].append({"parlay": p["id"], "team": leg["team"], "qty": n, "price": ask, "taker": True})
            if leg["contracts"] > 0:
                leg["status"] = "filled"
            else:
                leg["status"] = "skipped"
                if p["cur"] == 0:
                    close_parlay(state, p, "void", now_ts, "leg 1 never filled at a good price")
                else:
                    close_parlay(state, p, "banked", now_ts, f"leg {p['cur'] + 1} had no good price; kept winnings")
                return

        if leg["status"] == "planned":
            row = rows_by_ticker.get(leg["ticker"])
            if not row or row.get("q") is None:
                return                                  # wait for fresh odds (until the cutoff)
            price = wb.post_price(row["bid"], row["ask"], row["q"] - sc.LEG_MIN_EDGE)
            if price is None:
                return                                  # no edge right now; retry until the cutoff
            leg["q"] = row["q"]
            if not place_leg_order(p, leg, price, now_ts):
                close_parlay(state, p, "banked" if p["cur"] else "void", now_ts, "too little to place the next leg")
            return

        if leg["status"] == "ordered":
            row = rows_by_ticker.get(leg["ticker"])
            if row and row.get("q") is not None and leg["order"]["filled"] == 0:
                price = wb.post_price(row["bid"], row["ask"], row["q"] - sc.LEG_MIN_EDGE)
                if price is not None and abs(price - leg["order"]["price"]) > 0.004:
                    cancel_leg_order(p, leg)            # reprice (loses queue spot)
                    place_leg_order(p, leg, price, now_ts)
            return

        if leg["status"] == "filled":
            y = leg.get("result")
            if y is None:
                return
            p["chain_cash"] = round(p["chain_cash"] + leg["contracts"] * y, ROUND)
            leg["status"] = "won" if y >= 0.5 else "lost"
            if leg["status"] == "lost":
                close_parlay(state, p, "lost", now_ts)
                return
            p["cur"] += 1
            if p["cur"] >= len(p["legs"]):
                close_parlay(state, p, "won", now_ts)
                return
            continue                                    # immediately try to place the next leg
        return


def step_combo(state, p, now_ts):
    c = p["combo"]
    if c["status"] != "active":
        return
    results = [l.get("result") for l in p["legs"]]
    if any(r is not None and r < 0.5 for r in results):
        c.update(status="lost", payout=0.0, pnl=round(-c["stake"], ROUND))
    elif all(r is not None for r in results):
        payout = round(c["contracts"] * math.prod(results), ROUND)
        state["combo"]["cash"] = round(state["combo"]["cash"] + payout, ROUND)
        c.update(status="won", payout=payout, pnl=round(payout - c["stake"], ROUND))


# ---------------------------------------------------------------------------
# Scorecard: sportsbooks vs Kalshi
# ---------------------------------------------------------------------------
def update_scorecard(state, api, rows, now_ts, run):
    sco = state["scorecard"]
    pend = sco["pending"]
    for r in rows:
        if r.get("q") is not None and now_ts < r["start_ts"] and r.get("bid") is not None:
            pend[r["ticker"]] = {"q": r["q"], "mkt": r["mid"], "league": r["league"], "start_ts": r["start_ts"]}
    due = [t for t, v in pend.items() if now_ts > v["start_ts"] + 5 * 3600][:200]
    if not due:
        return
    try:
        res = api.markets_by_ticker(due)
    except Exception as e:  # noqa: BLE001
        run["errors"].append(f"scorecard: {e}")
        return
    for t in due:
        m = res.get(t)
        y = fav.settlement_payout(m, "yes") if m else None
        if y is None:
            if now_ts - pend[t]["start_ts"] > 7 * 86400:
                pend.pop(t)
            continue
        v = pend.pop(t)
        for key in ("all", v["league"]):
            tot = sco["totals"].setdefault(key, {"n": 0, "books": 0.0, "kalshi": 0.0})
            tot["n"] += 1
            tot["books"] += (v["q"] - y) ** 2
            tot["kalshi"] += (v["mkt"] - y) ** 2


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------
def run_cycle(api, data=None, now_ts=None, state_dir=None, verbose=True):
    state_dir = state_dir or sc.STATE_DIR
    now_ts = now_ts or time.time()
    os.makedirs(state_dir, exist_ok=True)
    sp, cp = os.path.join(state_dir, "portfolio.json"), os.path.join(state_dir, "cache.json")
    state = pf.load_json(sp, None) or new_state(now_ts)
    cache = pf.load_json(cp, {})
    data = data or sd.SportsData(cache, now_ts)
    if state.get("phase", {}).get("name") != sc.PHASE_NAME:
        state["phase"] = {"name": sc.PHASE_NAME, "start_ts": round(now_ts)}
    run = {"ts": round(now_ts), "error": None, "errors": [], "fills": [], "created": [], "board": [],
           "games": 0, "eligible": 0, "budget": None}
    t0 = time.time()
    try:
        # 1. Kalshi games per league
        games, markets = {}, {}
        for league, cfg in sc.LEAGUES.items():
            try:
                ms = api.markets_for_series(cfg["series"])
            except Exception as e:  # noqa: BLE001
                run["errors"].append(f"{league} markets: {e}")
                continue
            for m in ms:
                markets[m["ticker"]] = m
            for k, g in kalshi_games(ms, league, now_ts).items():
                if g["start_ts"] > now_ts - 6 * 3600 and g["start_ts"] < now_ts + sc.HORIZON_HOURS * 3600:
                    games[k] = g
        run["games"] = len(games)

        # 2. odds + ESPN for leagues with games coming up (spend credits only where needed)
        active_next = {}
        for p in state["parlays"]:
            if p["status"] == "active":
                leg = p["legs"][p["cur"]]
                if leg["start_ts"] - now_ts < 3 * 3600:
                    active_next[leg["league"]] = True
        leagues_needed = {g["league"] for g in games.values()}
        odds, espn = {}, {}
        et_now = datetime.fromtimestamp(now_ts, timezone.utc) - timedelta(hours=5)
        days = [(et_now + timedelta(days=d)).strftime("%Y%m%d") for d in (0, 1)]
        for league in leagues_needed:
            odds[league] = data.odds(league, force=active_next.get(league, False))
            espn[league] = [g for d in days for g in data.espn(league, d)]

        # 3. evaluate every game -> leg candidates
        rows = []
        for g in games.values():
            evs, ots = odds.get(g["league"], ([], None))
            rows += evaluate_game(g, evs, ots, espn.get(g["league"], []), now_ts)
        rows_by_ticker = {r["ticker"]: r for r in rows}
        run["eligible"] = sum(1 for r in rows if r["ok"])

        # 4. settle results for every leg of every active parlay (combos need all of them)
        need = {l["ticker"] for p in state["parlays"]
                if p["status"] == "active" or p["combo"]["status"] == "active" for l in p["legs"]}
        missing = sorted(need - set(markets))
        if missing:
            markets.update(api.markets_by_ticker(missing))
        for p in state["parlays"]:
            for leg in p["legs"]:
                m = markets.get(leg["ticker"])
                if leg.get("result") is None and m:
                    leg["result"] = fav.settlement_payout(m, "yes")

        # 5. advance each parlay
        for p in state["parlays"]:
            if p["status"] == "active":
                step_rolling(api, state, p, rows_by_ticker, markets, now_ts, run)
            step_combo(state, p, now_ts)

        # 6. start new parlays
        today = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%d")
        made_today = sum(1 for p in state["parlays"]
                         if datetime.fromtimestamp(p["created_ts"], timezone.utc).strftime("%Y-%m-%d") == today)
        used = {l["event"] for p in state["parlays"] if p["status"] == "active" for l in p["legs"]}
        active = sum(1 for p in state["parlays"] if p["status"] == "active")
        if rolling_equity(state) <= sc.FLOOR_EQUITY:
            run["halt"] = f"Rolling account at or below ${sc.FLOOR_EQUITY:.0f}: no new parlays."
        while active < sc.MAX_ACTIVE_PARLAYS and made_today < sc.MAX_NEW_PARLAYS_PER_DAY and not run.get("halt"):
            chain, ev = best_chain(rows, now_ts, used)
            if not chain:
                break
            p = create_parlay(state, chain, ev, now_ts, run)
            if not p:
                break
            used |= {c["event"] for c in chain}
            active += 1
            made_today += 1
            step_rolling(api, state, p, rows_by_ticker, markets, now_ts, run)   # post leg 1 right away

        update_scorecard(state, api, rows, now_ts, run)
        run["board"] = sorted(rows, key=lambda r: (r["start_ts"], r["event"]))[:120]
        data.prune()
    except Exception as e:  # noqa: BLE001
        run["error"] = f"{type(e).__name__}: {e}"

    run["budget"] = data.budget_info() if hasattr(data, "budget_info") else None
    run["health"] = getattr(data, "health", {})
    run["seconds"] = round(time.time() - t0, 1)
    state["rolling"]["history"].append([round(now_ts), rolling_equity(state), rolling_equity(state, mark=True)])
    state["combo"]["history"].append([round(now_ts), combo_equity(state), combo_equity(state)])
    for k in ("rolling", "combo"):
        state[k]["history"] = state[k]["history"][-5000:]
    done = [p for p in state["parlays"] if p["status"] != "active" and p["combo"]["status"] != "active"]
    if len(done) > 500:
        keep = {p["id"] for p in done[-500:]}
        state["parlays"] = [p for p in state["parlays"] if p["id"] in keep or p not in done]
    state["last_run"] = run
    pf.save_json(sp, state)
    pf.save_json(cp, cache)
    if verbose:
        print(f"[sports] {run['games']} games, {run['eligible']} eligible legs, {len(run['created'])} new parlays, "
              f"{len(run['fills'])} fills, rolling ${rolling_equity(state, True):.2f}, combo ${combo_equity(state):.2f}")
        if run["error"]:
            print("  ERROR: " + run["error"])
    return run["error"] is None
