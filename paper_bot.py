"""
Kalshi paper-trading bot. One run = one cycle:

  1. Check open bets: settle the ones Kalshi has decided, re-price the rest.
  2. Grade the watchlist (every qualifying market we've seen, bet or not).
  3. Scan markets closing in the next 15 min - 36 h and rank the favorites.
  4. Place imaginary bets that pass every risk rule.
  5. Save state and rebuild the HTML report.

Usage:
  python paper_bot.py               run one cycle (what GitHub Actions does)
  python paper_bot.py --loop 15     run forever, one cycle every 15 minutes
  python paper_bot.py --report      just rebuild the report from saved state
  python paper_bot.py --reset       wipe the paper account and start over at $100
"""
import argparse
import os
import shutil
import sys
import time
import urllib.error

import config
import portfolio
import report
import strategy
from kalshi_api import KalshiAPIError, KalshiPublicAPI


def refresh_open_positions(api, state, now_ts, log_path):
    opens = list(state["open_positions"])
    if not opens:
        return []
    markets = api.markets_by_ticker([p["ticker"] for p in opens])
    settled = []
    for pos in opens:
        m = markets.get(pos["ticker"])
        if m is None and now_ts > pos["close_ts"]:
            m = api.historical_market(pos["ticker"])
        if not m:
            continue
        payout = strategy.settlement_payout(m, pos["side"])
        if payout is not None:
            result = (m.get("result") or "settled").upper()
            settled.append(portfolio.close_position(state, pos, payout, result, now_ts, log_path))
        else:
            bid = strategy.side_quote(m, pos["side"])["bid"]
            # after trading closes the book empties; keep the last known mark
            if bid is not None:
                pos["mark_price"] = bid
                portfolio.check_shadow_stops(pos, bid, now_ts)
    return settled


def update_watchlist(api, watch, candidates, now_ts):
    entries = watch.setdefault("entries", {})
    added = 0
    for c in candidates:
        e = entries.get(c["ticker"])
        if e is not None:
            # still qualifies: remember its most recent price, so we can later
            # compare "price when first seen" vs "price near the close"
            if e["won"] is None and e["side"] == c["side"]:
                e["last_price"], e["last_ts"] = c["price"], round(now_ts)
            continue
        if added >= config.WATCHLIST_MAX_ADDS_PER_RUN:
            continue
        entries[c["ticker"]] = {
            "side": c["side"], "price": c["price"], "series": c["series"],
            "seen_ts": round(now_ts), "close_ts": c["close_ts"], "won": None,
            # extra detail for slicing results (volume, spread, topic)
            "vol24": round(c["volume_24h"]), "spread": c["spread"],
            "group": c.get("group"), "label": c["label"][:140],
        }
        added += 1

    due = [t for t, e in entries.items() if e["won"] is None and e["close_ts"] < now_ts]
    due = due[: config.WATCHLIST_MAX_CHECKS_PER_RUN]
    graded = 0
    if due:
        markets = api.markets_by_ticker(due)
        for t in due:
            e = entries[t]
            m = markets.get(t)
            payout = strategy.settlement_payout(m, e["side"]) if m else None
            if payout is not None:
                e["won"] = payout >= 0.5
                graded += 1
            elif now_ts - e["close_ts"] > 7 * 86400:
                e["won"] = "unknown"  # stop checking after a week
    return added, graded


def run_cycle(api, now_ts=None, state_dir=None, report_path=None, verbose=True):
    # folders are looked up at call time (not import time) so tests can't touch real state
    state_dir = state_dir or config.STATE_DIR
    report_path = report_path or config.REPORT_PATH
    now_ts = now_ts or time.time()
    os.makedirs(state_dir, exist_ok=True)
    log_path = os.path.join(state_dir, "trade_log.csv")
    watch_path = os.path.join(state_dir, "watchlist.json")
    state = portfolio.load_state(state_dir, now_ts)
    watch = portfolio.load_json(watch_path, {"entries": {}})
    portfolio.roll_day(state, now_ts)
    portfolio.update_phase(state, now_ts)
    state.setdefault("shadow_since", round(now_ts))  # when shadow stop tracking began

    run = {"ts": round(now_ts), "error": None, "markets_scanned": 0, "candidates": 0,
           "rejections": {}, "opened": [], "settled": [], "halt": None, "top_candidates": [],
           "skipped": {}}
    ok = True
    try:
        settled = refresh_open_positions(api, state, now_ts, log_path)
        run["settled"] = [p["id"] for p in settled]
        portfolio.update_peak(state)

        markets = api.markets_closing_between(
            now_ts + config.MIN_HOURS_TO_CLOSE * 3600, now_ts + config.MAX_HOURS_TO_CLOSE * 3600
        )
        candidates, rejections = strategy.find_candidates(markets, now_ts)
        run.update(markets_scanned=len(markets), candidates=len(candidates),
                   rejections=dict(rejections.most_common()),
                   top_candidates=[{k: c[k] for k in ("label", "side", "price", "hours_to_close",
                                                       "volume_24h", "score")}
                                   for c in candidates[:8]])
        run["watch_added"], run["watch_graded"] = update_watchlist(api, watch, candidates, now_ts)

        run["halt"] = portfolio.risk_halt_reason(state)
        if not run["halt"]:
            skips = {}
            opened = portfolio.place_trades(state, candidates, now_ts, log_path, skips)
            run["opened"] = [p["id"] for p in opened]
            run["skipped"] = skips
    except (KalshiAPIError, urllib.error.URLError, TimeoutError, OSError) as e:
        run["error"] = f"{type(e).__name__}: {e}"
        ok = False

    portfolio.record_equity(state, now_ts)
    run["api_calls"] = getattr(api, "calls", None)
    state["last_run"] = run
    portfolio.save_state(state, state_dir)
    portfolio.save_json(watch_path, watch)
    report.write_report(state, watch, report_path, now_ts)

    if verbose:
        print(f"[{portfolio.utc_iso(now_ts)}] scanned {run['markets_scanned']} markets, "
              f"{run['candidates']} qualified, opened {len(run['opened'])}, "
              f"settled {len(run['settled'])}")
        print(f"  equity ${portfolio.mark_equity(state):.2f} (book ${portfolio.book_equity(state):.2f}), "
              f"cash ${state['cash']:.2f}, open bets {len(state['open_positions'])}")
        if run["halt"]:
            print("  BRAKE ON: " + run["halt"])
        if run["error"]:
            print("  ERROR: " + run["error"])
    return ok


def run_all(api, now_ts=None):
    """One scheduled run: favorites bot, then weather bot, then one combined dashboard.
    A weather-bot failure is reported on its tab and never affects the favorites bot."""
    now_ts = now_ts or time.time()
    ok = run_cycle(api, now_ts, config.STATE_DIR, config.REPORT_PATH)
    for name in ("weather_bot", "sports_bot"):
        try:
            __import__(name).run_cycle(api, now_ts=now_ts)
        except Exception as e:  # noqa: BLE001  (one bot failing never stops the others)
            print(f"[{name.split('_')[0]}] crashed: {type(e).__name__}: {e}")
    state = portfolio.load_state(config.STATE_DIR, now_ts)
    watch = portfolio.load_json(os.path.join(config.STATE_DIR, "watchlist.json"), {"entries": {}})
    report.write_report(state, watch, config.REPORT_PATH, now_ts)
    return ok


def _backup(paths, label):
    backup = f"{config.STATE_DIR}_backup_{label}_{int(time.time())}"
    moved = False
    for p in paths:
        if os.path.exists(p):
            os.makedirs(backup, exist_ok=True)
            shutil.move(p, os.path.join(backup, os.path.basename(p)))
            moved = True
    if moved:
        print(f"Old state moved to {backup}")


def main():
    ap = argparse.ArgumentParser(description="Kalshi paper-trading bots (favorites + weather + sports)")
    ap.add_argument("--loop", type=float, metavar="MINUTES", help="repeat every N minutes")
    ap.add_argument("--report", action="store_true", help="rebuild the dashboard only")
    ap.add_argument("--reset", action="store_true", help="start a fresh $100 FAVORITES paper account")
    ap.add_argument("--reset-weather", action="store_true", help="start a fresh $100 WEATHER paper account")
    ap.add_argument("--reset-sports", action="store_true", help="start fresh $100 SPORTS paper accounts")
    ap.add_argument("--favorites-only", action="store_true", help="skip the weather bot this run")
    args = ap.parse_args()

    if args.reset or args.reset_weather or args.reset_sports:
        if args.reset:
            _backup([os.path.join(config.STATE_DIR, f) for f in
                     ("portfolio.json", "watchlist.json", "trade_log.csv")], "favorites")
        if args.reset_weather:
            import weather_config
            _backup([weather_config.STATE_DIR], "weather")
        if args.reset_sports:
            import sports_config
            _backup([sports_config.STATE_DIR], "sports")
        print("Fresh paper account(s) created.")
        return 0

    if args.report:
        now = time.time()
        state = portfolio.load_state(config.STATE_DIR, now)
        watch = portfolio.load_json(os.path.join(config.STATE_DIR, "watchlist.json"), {"entries": {}})
        report.write_report(state, watch, config.REPORT_PATH)
        print(f"Report written to {config.REPORT_PATH}")
        return 0

    api = KalshiPublicAPI()
    once = (lambda: run_cycle(api)) if args.favorites_only else (lambda: run_all(api))
    if args.loop:
        print(f"Running every {args.loop:g} minutes. Ctrl+C to stop.")
        while True:
            try:
                once()
            except Exception as e:  # never let one bad cycle kill an unattended loop
                print(f"  Unexpected error, will retry next cycle: {type(e).__name__}: {e}")
            time.sleep(args.loop * 60)
    return 0 if once() else 1


if __name__ == "__main__":
    sys.exit(main())
