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
import report
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
        st["cash"] = 80.0  # pretend we lost $20 today (past the 15% daily stop)
        st["phase"] = {"name": config.PHASE_NAME, "start_ts": NOW - 3600,
                       "start_equity": 100.0, "start_book": 100.0}
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


class TestRelatedGroups(unittest.TestCase):
    def test_real_titles_from_live_dashboard(self):
        cases = {
            "Will CPI Core rise more than 0.3% in September? — Above 0.3%": "Inflation reports",
            "Will CPI rise more than 0.4% in September 2026? — Above 0.4%": "Inflation reports",
            "Will the rate of CPI inflation be above 3.5% for the year ending in September 2026?":
                "Inflation reports",
            "Will the WTI crude oil settlement price be above 90.49 USD/Bbl on Oct 13, 2026?":
                "Oil prices",
            "Will the brent crude oil close price be above 100 USD/Bbl on October 13, 2026?":
                "Oil prices",
            "Bitcoin price on Oct 13, 2026? — $84,500 or above": "Crypto",
            "Will Trump's RCP approval average be exactly 38.4 on Oct 13, 2026?":
                "Presidential approval",
            "Will average **gas prices** be above $4.3700? — Above 4.3700": "Gas prices",
            "Will the gold close price be above 4129 USD/t.oz on October 12, 2026?":
                "Precious metals",
            "Will October 11 be the day with the most transit calls through the Strait of Hormuz?":
                "Strait of Hormuz",
        }
        for title, group in cases.items():
            with self.subTest(title):
                self.assertEqual(strategy.related_group(title), group)

    def test_no_false_alarms(self):
        for title in ["Will it rain in New York City on Oct 13, 2026?",
                      "Golden State Warriors wins", "Sporting Jax wins",
                      "Will Julia Stiles be eliminated from Dancing with the Stars?",
                      "Will the maximum temperature be 73-74° on Oct 13, 2026?",
                      "Showdown at the stadium: who wins?"]:
            with self.subTest(title):
                self.assertIsNone(strategy.related_group(title))


class TestNewRiskFeatures(Base):
    def test_one_cpi_bet_not_three(self):
        self.api.add("KXCPICORE-26SEP-T0.3", NOW + 5 * H, 0.06, 0.07,
                     title="Will CPI Core rise more than 0.3% in September?")
        self.api.add("KXCPI-26SEP-T0.4", NOW + 5 * H, 0.91, 0.92,
                     title="Will CPI rise more than 0.4% in September 2026?")
        self.api.add("KXCPIYOY-26SEP-T3.5", NOW + 5 * H, 0.87, 0.88,
                     title="Will the rate of CPI inflation be above 3.5%?")
        self.api.add("KXRAINNYC-26OCT13", NOW + 5 * H, 0.06, 0.07,
                     title="Will it rain in New York City on Oct 13, 2026?")
        self.run_cycle(NOW)
        st = self.state()
        labels = [p["label"] for p in st["open_positions"]]
        self.assertEqual(sum("CPI" in l for l in labels), 1)
        self.assertTrue(any("rain" in l for l in labels))  # unrelated bets still go through
        self.assertEqual(st["last_run"]["skipped"], {"related-market cap (Inflation reports)": 2})
        with open(self.report, encoding="utf-8") as f:
            self.assertIn("related-market cap (Inflation reports) ×2", f.read())

    def test_cap_counts_bets_already_open(self):
        self.api.add("KXWTI-26OCT13-T90", NOW + 5 * H, 0.91, 0.92,
                     title="Will the WTI crude oil settlement price be above 90.49?")
        self.run_cycle(NOW)
        self.api.add("KXBRENTD-26OCT13-T100", NOW + 6 * H, 0.94, 0.95,
                     title="Will the brent crude oil close price be above 100?")
        self.run_cycle(NOW + 900)
        self.assertEqual(len(self.state()["open_positions"]), 1)

    def test_shadow_stop_records_but_never_sells(self):
        t = "KXKAT-26OCT11-T20"
        self.api.add(t, NOW + 20 * H, 0.08, 0.10, title="Will KATSEYE have above 20M views?")
        self.run_cycle(NOW)                                   # buys NO at 92c
        pos = self.state()["open_positions"][0]
        self.assertAlmostEqual(pos["price"], 0.92)
        self.assertEqual(pos["shadow"], {})

        self.api.set_quote(t, 0.24, 0.26)                     # NO bid falls to 74c (-18c)
        self.run_cycle(NOW + 900)
        st = self.state()
        self.assertEqual(len(st["open_positions"]), 1)        # still holding: nothing sold
        sh = st["open_positions"][0]["shadow"]
        self.assertEqual(sorted(sh), ["10", "15"])            # 20c and 30c not reached
        self.assertAlmostEqual(sh["10"]["bid"], 0.74)
        n = pos["contracts"]
        self.assertAlmostEqual(sh["10"]["proceeds"],
                               n * 0.74 - strategy.taker_fee(n, 0.74), places=4)

        self.api.set_quote(t, 0.40, 0.42)                     # falls further to 58c
        self.run_cycle(NOW + 1800)
        sh = self.state()["open_positions"][0]["shadow"]
        self.assertAlmostEqual(sh["10"]["bid"], 0.74)         # first trigger price kept
        self.assertEqual(sorted(sh), ["10", "15", "20", "30"])

        self.api.settle(t, "yes")                             # our NO side loses
        self.run_cycle(NOW + 21 * H)
        closed = self.state()["closed_positions"][0]
        self.assertAlmostEqual(closed["payout"], 0.0)
        rows = {r["drop"]: r for r in report.shadow_stats(self.state())[0]}
        self.assertEqual(rows[0.10]["helped"], 1)
        self.assertAlmostEqual(rows[0.10]["net"], sh["10"]["proceeds"])
        self.assertEqual(rows[0.10]["verdict"], "Would have helped")

    def test_shadow_stop_cost_when_bet_recovers(self):
        t = "KXDIP-26OCT13-A"
        self.api.add(t, NOW + 20 * H, 0.89, 0.90)
        self.run_cycle(NOW)
        self.api.set_quote(t, 0.78, 0.80)                     # dips 12c...
        self.run_cycle(NOW + 900)
        self.api.settle(t, "yes")                             # ...but wins anyway
        self.run_cycle(NOW + 21 * H)
        r = report.shadow_stats(self.state())[0][0]
        self.assertEqual((r["fired"], r["hurt"]), (1, 1))
        self.assertLess(r["net"], 0)
        self.assertEqual(r["verdict"], "Would have hurt")

    def test_no_shadow_trigger_after_trading_closes(self):
        t = "KXLATE-26OCT13-A"
        self.api.add(t, NOW + 2 * H, 0.89, 0.90)
        self.run_cycle(NOW)
        self.api.set_quote(t, 0.20, 0.22)
        self.run_cycle(NOW + 3 * H)                           # after close: can't sell
        self.assertEqual(self.state()["open_positions"][0]["shadow"], {})

    def test_old_state_without_new_fields_still_works(self):
        self.api.add("KXOLD-26OCT13-A", NOW + 5 * H, 0.89, 0.90)
        self.run_cycle(NOW)
        st = self.state()
        for p in st["open_positions"]:
            p.pop("shadow")                                   # simulate last week's bets
        st.pop("shadow_since")
        portfolio.save_state(st, self.state_dir)
        self.api.set_quote("KXOLD-26OCT13-A", 0.20, 0.22)
        self.assertTrue(self.run_cycle(NOW + 900))
        st = self.state()
        self.assertNotIn("shadow", st["open_positions"][0])   # untracked, not mis-counted
        self.assertIn("shadow_since", st)


class TestWeek2Changes(Base):
    def test_80_85c_tracked_but_not_bet(self):
        self.api.add("KXLOW-26OCT15-A", NOW + 5 * H, 0.81, 0.82)   # 82c: track only
        self.api.add("KXOK-26OCT15-A", NOW + 5 * H, 0.87, 0.88)    # 88c: bet
        self.run_cycle(NOW)
        st = self.state()
        self.assertEqual([p["ticker"] for p in st["open_positions"]], ["KXOK-26OCT15-A"])
        watch = portfolio.load_json(os.path.join(self.state_dir, "watchlist.json"), {})
        self.assertIn("KXLOW-26OCT15-A", watch["entries"])           # evidence still collected
        self.assertEqual(st["last_run"]["skipped"], {"tracked only (below bet price floor)": 1})

    def test_bet_size_and_exposure_at_new_settings(self):
        for i in range(30):
            self.api.add(f"KXS{i}-26OCT15-A", NOW + 5 * H, 0.89, 0.90)
        for k in range(10):
            self.run_cycle(NOW + k * 900)
        st = self.state()
        costs = [p["cost"] for p in st["open_positions"]]
        self.assertTrue(all(c <= 100 * config.RISK_PER_TRADE + 1e-9 for c in costs))
        self.assertGreater(max(costs), 100 * config.RISK_PER_TRADE * 0.85)   # actually ~6%
        self.assertLessEqual(sum(costs), 100 * config.MAX_TOTAL_EXPOSURE + 1e-9)
        self.assertGreater(sum(costs), 100 * config.MAX_TOTAL_EXPOSURE * 0.9)  # cash put to work

    def test_phase_starts_once_and_restarts_on_rename(self):
        self.api.add("KXA-26OCT15-A", NOW + 5 * H, 0.89, 0.90)
        self.run_cycle(NOW)
        self.run_cycle(NOW + 900)
        st = self.state()
        self.assertEqual(st["phase"]["name"], config.PHASE_NAME)
        self.assertEqual(st["phase"]["start_ts"], NOW)               # not reset on later runs
        self.api.settle("KXA-26OCT15-A", "yes")
        self.run_cycle(NOW + 6 * H)
        ph = portfolio.phase_stats(self.state())
        self.assertEqual((ph["wins"], ph["losses"]), (1, 0))
        self.assertGreater(ph["pnl"], 0)

        self.cfg(PHASE_NAME="Week 3: something new")
        self.run_cycle(NOW + 7 * H)
        st = self.state()
        self.assertEqual(st["phase"]["name"], "Week 3: something new")
        self.assertEqual(st["past_phases"][0]["name"], self._saved["PHASE_NAME"])
        self.assertEqual(len(st["past_phases"]), 1)
        self.assertEqual(portfolio.phase_stats(st)["wins"], 0)       # fresh scoreboard
        with open(self.report, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("This phase", page)
        self.assertIn("Week 3: something new", page)

    def test_old_week1_state_upgrades_cleanly(self):
        # a state file exactly like week 1's: no phase, positions without new fields
        st = portfolio.new_state(NOW - 7 * 86400)
        st["peak_equity"] = 107.65
        portfolio.save_state(st, self.state_dir)
        self.api.add("KXA-26OCT15-A", NOW + 5 * H, 0.89, 0.90)
        self.assertTrue(self.run_cycle(NOW))
        st = self.state()
        self.assertEqual(st["phase"]["start_ts"], NOW)
        self.assertEqual(len(st["open_positions"]), 1)

    def test_watchlist_records_detail_and_latest_price(self):
        t = "KXW-26OCT15-A"
        self.api.add(t, NOW + 20 * H, 0.89, 0.90, vol24=5000, title="Will gold close above 4000?")
        self.run_cycle(NOW)
        self.api.set_quote(t, 0.92, 0.93)
        self.run_cycle(NOW + 900)
        e = portfolio.load_json(os.path.join(self.state_dir, "watchlist.json"), {})["entries"][t]
        self.assertEqual((e["price"], e["last_price"]), (0.90, 0.93))
        self.assertEqual(e["vol24"], 5000)
        self.assertEqual(e["group"], "Precious metals")
        self.assertIn("gold", e["label"])


if __name__ == "__main__":
    unittest.main()
