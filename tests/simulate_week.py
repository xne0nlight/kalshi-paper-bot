"""
Plumbing test: run the real bot code against a FAKE market for a simulated week
(672 cycles = every 15 minutes) and write sim_output/index.html.

Outcomes are coin flips weighted by each market's true probability, which is
the market price plus --edge. With --edge 0 (the default) favorites win exactly
as often as their price says, so after fees you should expect a small LOSS.
That's the honest baseline the live paper test has to beat.

  python tests/simulate_week.py                 # no edge
  python tests/simulate_week.py --edge 0.02     # favorites win 2 pts more often than priced
"""
import argparse
import os
import random
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_kalshi import FakeKalshi  # noqa: E402

import paper_bot  # noqa: E402
import strategy  # noqa: E402

SERIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME",
          "KXBTCD", "KXETHD", "KXINXD", "KXTSAW", "KXGASD", "KXNBAGAME"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=float, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "sim_output")
    shutil.rmtree(out, ignore_errors=True)
    state_dir = os.path.join(out, "state")

    api = FakeKalshi()
    truth = {}
    now = 1_791_460_800  # a Thursday morning
    serial = 0
    for _ in range(int(args.days * 96)):
        # new markets appear
        for _ in range(rng.randint(3, 9)):
            serial += 1
            s = rng.choice(SERIES)
            close = now + rng.uniform(0.5, 40) * 3600
            fav = rng.uniform(0.55, 0.97)
            spread = rng.choice([0.01, 0.01, 0.02, 0.02, 0.03, 0.05])
            yes_is_fav = rng.random() < 0.5
            ask = round(min(fav, 0.99), 2)
            bid = round(max(ask - spread, 0.01), 2)
            if not yes_is_fav:
                bid, ask = round(1 - ask, 2), round(1 - bid, 2)
            t = f"{s}-{serial:05d}-A"
            api.add(t, close, bid, ask, title=f"Sim {s} #{serial}",
                    vol24=int(rng.lognormvariate(6.5, 1.3)))
            p_fav = min(fav + args.edge, 0.999)
            truth[t] = ("yes" if yes_is_fav else "no") if rng.random() < p_fav else \
                       ("no" if yes_is_fav else "yes")
        # markets that closed get settled
        for t, m in api.markets.items():
            if m["status"] == "active" and strategy.parse_ts(m["close_time"]) < now:
                api.settle(t, truth[t])
        paper_bot.run_cycle(api, now, state_dir, os.path.join(out, "index.html"), verbose=False)
        now += 900

    st = paper_bot.portfolio.load_state(state_dir, now)
    closed = st["closed_positions"]
    wins = sum(1 for p in closed if p["won"])
    print(f"Simulated {args.days:g} days with edge {args.edge:+.3f}")
    print(f"  bets settled: {len(closed)}  record {wins}-{len(closed) - wins}")
    print(f"  final value ${paper_bot.portfolio.mark_equity(st):.2f}  "
          f"fees ${st['fees_paid']:.2f}")
    print(f"  report: {os.path.join(out, 'index.html')}")


if __name__ == "__main__":
    main()
