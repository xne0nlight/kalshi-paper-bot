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
    return settled


def update_watchlist(api, watch, candidates, now_ts):
    entries = watch.setdefault("entries", {})
    added = 0
    for c in candidates:
        if added >= config.WATCHLIST_MAX_ADDS_PER_RUN:
            break
        if c["ticker"] in entries:
            continue
        entries[c["ticker"]] = {
            "side": c["side"], "price": c["price"], "series": c["series"],
            "seen_ts": round(now_ts), "close_ts": c["close_ts"], "won": None,
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


def run_cycle(api, now_ts=None, state_dir=config.STATE_DIR, report_path=config.REPORT_PATH,
              verbose=True):
    now_ts = now_ts or time.time()
    os.makedirs(state_dir, exist_ok=True)
    log_path = os.path.join(state_dir, "trade_log.csv")
    watch_path = os.path.join(state_dir, "watchlist.json")
    state = portfolio.load_state(state_dir, now_ts)
    watch = portfolio.load_json(watch_path, {"entries": {}})
    portfolio.roll_day(state, now_ts)

    run = {"ts": round(now_ts), "error": None, "markets_scanned": 0, "candidates": 0,
           "rejections": {}, "opened": [], "settled": [], "halt": None, "top_candidates": []}
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
            opened = portfolio.place_trades(state, candidates, now_ts, log_path)
            run["opened"] = [p["id"] for p in opened]
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


def main():
    ap = argparse.ArgumentParser(description="Kalshi paper-trading bot")
    ap.add_argument("--loop", type=float, metavar="MINUTES", help="repeat every N minutes")
    ap.add_argument("--report", action="store_true", help="rebuild the report only")
    ap.add_argument("--reset", action="store_true", help="start a fresh $100 paper account")
    args = ap.parse_args()

    if args.reset:
        if os.path.isdir(config.STATE_DIR):
            backup = f"{config.STATE_DIR}_backup_{int(time.time())}"
            shutil.move(config.STATE_DIR, backup)
            print(f"Old state moved to {backup}")
        print("Fresh paper account created.")
        return 0

    if args.report:
        now = time.time()
        state = portfolio.load_state(config.STATE_DIR, now)
        watch = portfolio.load_json(os.path.join(config.STATE_DIR, "watchlist.json"), {"entries": {}})
        report.write_report(state, watch, config.REPORT_PATH)
        print(f"Report written to {config.REPORT_PATH}")
        return 0

    api = KalshiPublicAPI()
    if args.loop:
        print(f"Running every {args.loop:g} minutes. Ctrl+C to stop.")
        while True:
            try:
                run_cycle(api)
            except Exception as e:  # never let one bad cycle kill an unattended loop
                print(f"  Unexpected error, will retry next cycle: {type(e).__name__}: {e}")
            time.sleep(args.loop * 60)
    return 0 if run_cycle(api) else 1


if __name__ == "__main__":
    sys.exit(main())
