"""Offline tests. Run from the project folder:  python -m unittest discover tests -v"""
import csv
import os
import shutil
import tempfile
import unittest

from fake_kalshi import FakeKalshi, make_market

import config
import paper_bot
import portfolio
import strategy

NOW = 1_791_500_000  # a fixed fake clock
H = 3600


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.dir, "state")
        self.report = os.path.join(self.dir, "docs", "index.html")
        self.api = FakeKalshi()
        self._saved = {}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.dir, ignore_errors=True)

    def cfg(self, **kw):
        for k, v in kw.items():
            self._saved.setdefault(k, getattr(config, k))
            setattr(config, k, v)

    def run_cycle(self, now):
        return paper_bot.run_cycle(self.api, now, self.state_dir, self.report, verbose=False)

    def state(self):
        return portfolio.load_json(os.path.join(self.state_dir, "portfolio.json"), None)


class TestFees(unittest.TestCase):
    def test_kalshi_published_example(self):
        self.assertAlmostEqual(strategy.taker_fee(100, 0.55), 1.74)

    def test_rounds_up_to_cent(self):
        self.assertAlmostEqual(strategy.taker_fee(10, 0.90), 0.07)   # 0.063 -> 0.07
        self.assertAlmostEqual(strategy.taker_fee(1, 0.50), 0.02)    # 0.0175 -> 0.02
        self.assertAlmostEqual(strategy.taker_fee(100, 0.90), 0.63)  # exact stays exact


class TestScreening(unittest.TestCase):
    def ev(self, **kw):
        defaults = dict(ticker="KXTEST-26OCT08-A", close_ts=NOW + 6 * H, yes_bid=0.88, yes_ask=0.90)
        defaults.update(kw)
        return strategy.evaluate(make_market(**defaults), NOW)

    def test_picks_yes_favorite(self):
        c, why = self.ev()
        self.assertIsNone(why)
        self.assertEqual((c["side"], c["price"]), ("yes", 0.90))

    def test_picks_no_side_when_yes_is_the_longshot(self):
        c, why = self.ev(yes_bid=0.08, yes_ask=0.10)
        self.assertIsNone(why)
        self.assertEqual(c["side"], "no")
        self.assertAlmostEqual(c["price"], 0.92)
        self.assertAlmostEqual(c["bid"], 0.90)

    def test_rejections(self):
        cases = {
            "spread too wide": dict(yes_bid=0.80, yes_ask=0.90),
            "too little volume": dict(vol24=20),
            "outside time window": dict(close_ts=NOW + 60 * H),
            "no favorite in 80-95c band": dict(yes_bid=0.55, yes_ask=0.57),
            "excluded series (combo/parlay)": dict(ticker="KXMVECROSS-1-A"),
            "favorite is slipping fast": dict(prev=(0.97, 0.99)),
            "nothing for sale at the ask": dict(ask_size=0),
            "not a yes/no market": dict(market_type="scalar"),
        }
        for reason, kw in cases.items():
            with self.subTest(reason):
                c, why = self.ev(**kw)
                self.assertIsNone(c)
                self.assertEqual(why, reason)

    def test_too_expensive_rejected(self):
        c, why = self.ev(yes_bid=0.97, yes_ask=0.98)
        self.assertEqual(why, "no favorite in 80-95c band")

    def test_legacy_cent_fields_still_parse(self):
        m = {"ticker": "X-1", "yes_ask": 90, "yes_bid": 88}
        self.assertAlmostEqual(strategy.dollars(m, "yes_ask"), 0.90)

    def test_parse_fractional_timestamps(self):
        a = strategy.parse_ts("2026-10-08T15:00:00Z")
        b = strategy.parse_ts("2026-10-08T15:00:00.1234567Z")
        self.assertAlmostEqual(a, b, delta=1)


class TestSizing(unittest.TestCase):
    def test_five_percent_of_100(self):
        n = portfolio.size_order(5.00, 0.90, 10_000)
        self.assertEqual(n, 5)
        self.assertLessEqual(5 * 0.90 + strategy.taker_fee(5, 0.90), 5.00)

    def test_respects_available_size(self):
        self.assertEqual(portfolio.size_order(50, 0.90, 3), 3)

    def test_zero_budget(self):
        self.assertEqual(portfolio.size_order(0, 0.90, 100), 0)


class TestCycle(Base):
    def add_basket(self):
        # 5 good markets in 5 different series/events
        for i, s in enumerate(["KXA", "KXB", "KXC", "KXD", "KXE"]):
            self.api.add(f"{s}-26OCT08-T{i}", NOW + (3 + i) * H, 0.88, 0.90)

    def test_open_then_settle(self):
        self.add_basket()
        self.assertTrue(self.run_cycle(NOW))
        st = self.state()
        self.assertEqual(len(st["open_positions"]), config.MAX_NEW_TRADES_PER_RUN)

        self.run_cycle(NOW + 900)
        st = self.state()
        self.assertEqual(len(st["open_positions"]), 5)
        spent = sum(p["cost"] for p in st["open_positions"])
        self.assertAlmostEqual(st["cash"], 100 - spent, places=4)

        # four favorites win, one loses
        tickers = [p["ticker"] for p in st["open_positions"]]
        for t in tickers[:4]:
            self.api.settle(t, "yes")
        self.api.settle(tickers[4], "no")
        self.run_cycle(NOW + 10 * H)
        st = self.state()
        self.assertEqual(len(st["open_positions"]), 0)
        self.assertEqual(len(st["closed_positions"]), 5)
        pnl = sum(p["pnl"] for p in st["closed_positions"])
        self.assertAlmostEqual(st["cash"], 100 + pnl, places=4)
        self.assertEqual(sum(1 for p in st["closed_positions"] if p["won"]), 4)
        # every trade logged twice (OPEN + SETTLE)
        with open(os.path.join(self.state_dir, "trade_log.csv"), encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 10)
        with open(self.report, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("WON", page)
        self.assertIn("LOST", page)

    def test_no_side_settlement(self):
        self.api.add("KXNO-26OCT08-A", NOW + 3 * H, 0.08, 0.10)  # we buy NO at 92c
        self.run_cycle(NOW)
        pos = self.state()["open_positions"][0]
        self.assertEqual(pos["side"], "no")
        self.api.settle("KXNO-26OCT08-A", "no")  # NO wins
        self.run_cycle(NOW + 5 * H)
        closed = self.state()["closed_positions"][0]
        self.assertTrue(closed["won"])
        self.assertAlmostEqual(closed["payout"], pos["contracts"] * 1.0)

    def test_one_bet_per_event(self):
        for strike in ("T70", "T71", "T72"):
            self.api.add(f"KXHIGH-26OCT08-{strike}", NOW + 4 * H, 0.88, 0.90, event="KXHIGH-26OCT08")
        self.run_cycle(NOW)
        self.assertEqual(len(self.state()["open_positions"]), 1)

    def test_series_cap(self):
        for d in ("26OCT08", "26OCT09", "26OCT10"):
            self.api.add(f"KXHIGHNY-{d}-T70", NOW + 4 * H, 0.88, 0.90)
        self.run_cycle(NOW)
        self.assertEqual(len(self.state()["open_positions"]), config.MAX_POSITIONS_PER_SERIES)

    def test_exposure_cap(self):
        self.cfg(MAX_NEW_TRADES_PER_RUN=50, MAX_OPEN_POSITIONS=50, RISK_PER_TRADE=0.10)
        for i in range(30):
            self.api.add(f"KXS{i}-26OCT08-A", NOW + 4 * H, 0.88, 0.90)
        self.run_cycle(NOW)
        st = self.state()
        self.assertLessEqual(sum(p["cost"] for p in st["open_positions"]),
                             100 * config.MAX_TOTAL_EXPOSURE + 1e-9)
        self.assertGreaterEqual(st["cash"], 100 * (1 - config.MAX_TOTAL_EXPOSURE) - 1e-9)

    def test_daily_stop_blocks_new_bets(self):
        self.add_basket()
        os.makedirs(self.state_dir, exist_ok=True)
        st = portfolio.new_state(NOW)
        st["cash"] = 90.0  # pretend we lost $10 today
        portfolio.save_state(st, self.state_dir)
        self.run_cycle(NOW)
        st = self.state()
        self.assertEqual(st["open_positions"], [])
        self.assertIn("Daily stop-loss", st["last_run"]["halt"])

    def test_new_day_resets_daily_stop(self):
        self.add_basket()
        os.makedirs(self.state_dir, exist_ok=True)
        st = portfolio.new_state(NOW - 86400)
        st["cash"] = 90.0
        st["peak_equity"] = 100.0
        portfolio.save_state(st, self.state_dir)
        self.run_cycle(NOW)  # next UTC day: day_start becomes 90, so trading resumes
        self.assertGreater(len(self.state()["open_positions"]), 0)

    def test_watchlist_grades_everything_seen(self):
        self.add_basket()
        self.run_cycle(NOW)
        for t in list(self.api.markets):
            self.api.settle(t, "yes")
        self.run_cycle(NOW + 10 * H)
        watch = portfolio.load_json(os.path.join(self.state_dir, "watchlist.json"), {})
        self.assertEqual(len(watch["entries"]), 5)
        self.assertTrue(all(e["won"] is True for e in watch["entries"].values()))

    def test_api_failure_is_recorded_not_fatal(self):
        class Broken(FakeKalshi):
            def markets_closing_between(self, a, b):
                raise OSError("network down")
        self.api = Broken()
        self.assertFalse(self.run_cycle(NOW))
        self.assertIn("network down", self.state()["last_run"]["error"])
        self.assertTrue(os.path.exists(self.report))


if __name__ == "__main__":
    unittest.main()
