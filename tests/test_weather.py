"""Offline tests for the weather bot. Run: python -m unittest discover tests -v"""
import math
import os
import random
import shutil
import tempfile
import unittest

from fake_kalshi import FakeKalshi, iso, make_market

import paper_bot
import portfolio
import report
import weather_bot as wb
import weather_config as wc
import weather_data as wd
import weather_model as wm

# Mon Oct 19 2026, 5:00 PM in New York (EDT) = 21:00 UTC. Markets are for Tue Oct 20.
NOW = 1792443600
H = 3600
CLOSE = 1792558800          # 2026-10-21 05:00 UTC (1 AM ET after the event day)
DAY_START = 1792472400      # 2026-10-20 05:00 UTC = midnight EST (NWS climate day)


def wx_market(api, suffix, strike_type, yes_bid, yes_ask, floor=None, cap=None, code="NYC",
              event="KXHIGHNY-26OCT20", kind="maximum"):
    t = f"{event}-{suffix}"
    m = api.add(t, CLOSE, yes_bid, yes_ask, event=event, title=f"{kind} temp {suffix}", vol24=800)
    m["strike_type"] = strike_type
    if floor is not None:
        m["floor_strike"] = floor
    if cap is not None:
        m["cap_strike"] = cap
    m["rules_primary"] = (f"If the {kind} temperature recorded at New York City (CLI{code}) for Oct 20, 2026, "
                          f"is ... according to the Weather Company")
    return m


class FakeData:
    """Stands in for weather_data.WeatherData: centered-66°F forecast for Oct 20 in NYC."""
    def __init__(self, center=66.0, observed=None):
        rng = random.Random(7)
        self.ext = {"ecmwf": [round(rng.gauss(center, 1.4), 1) for _ in range(51)],
                    "gfs": [round(rng.gauss(center, 1.6), 1) for _ in range(31)]}
        self.observed = observed
        self.health = {"open_meteo_ensemble": {"ok": 2, "fail": 0, "last_error": None}}

    def station(self, code):
        return {"lat": 40.78, "lon": -73.97, "std_offset": -5, "dst": True, "code": code, "name": "Central Park"}

    def forecast_extremes(self, code, st):
        return {"2026-10-20": {"high": self.ext}}

    def nbm_extremes(self, code, st):
        return {"2026-10-20": {"high": 66.0}}

    def observed_so_far(self, code, start_ts):
        return (self.observed, None)

    def climate_actuals(self, code, year):
        return {}

    def calibration(self, code, st):
        return {"high": {"bias": 0.0, "sd": 1.5, "n": 28}}


def ladder(api):
    """A realistic Oct-20 NYC high ladder (yes bid/ask)."""
    wx_market(api, "T60", "less", 0.01, 0.02, cap=60)
    wx_market(api, "B60.5", "between", 0.02, 0.03, floor=60, cap=61)
    wx_market(api, "B62.5", "between", 0.06, 0.08, floor=62, cap=63)
    wx_market(api, "B64.5", "between", 0.25, 0.27, floor=64, cap=65)
    wx_market(api, "B66.5", "between", 0.38, 0.40, floor=66, cap=67)
    wx_market(api, "B68.5", "between", 0.22, 0.24, floor=68, cap=69)
    wx_market(api, "B70.5", "between", 0.05, 0.07, floor=70, cap=71)
    wx_market(api, "T71", "greater", 0.01, 0.02, floor=71)


# ---------------------------------------------------------------------------
class TestModel(unittest.TestCase):
    def test_bracket_semantics_match_live_kalshi(self):
        self.assertEqual(wm.bracket_bounds({"strike_type": "greater", "floor_strike": 67}), (67.5, math.inf))
        self.assertEqual(wm.bracket_bounds({"strike_type": "less", "cap_strike": 60}), (-math.inf, 59.5))
        self.assertEqual(wm.bracket_bounds({"strike_type": "between", "floor_strike": 64, "cap_strike": 65}),
                         (63.5, 65.5))
        self.assertEqual(wm.bracket_label(63.5, 65.5), "64–65°")
        self.assertEqual(wm.bracket_label(67.5, math.inf), "≥68°")

    def test_probabilities_sum_to_one_and_obs_floor(self):
        d = FakeData()
        comps = wm.build_components(d.ext, 0.0, 1.3, 66.0)
        edges = [-math.inf, 59.5, 61.5, 63.5, 65.5, 67.5, 69.5, 71.5, math.inf]
        ps = [wm.prob_in(comps, a, b) for a, b in zip(edges, edges[1:])]
        self.assertAlmostEqual(sum(ps), 1.0, places=6)
        self.assertGreater(ps[4], ps[0])                    # 66-67 far likelier than <=59
        seen = [wm.prob_in(comps, a, b, "high", observed=70.0) for a, b in zip(edges, edges[1:])]
        self.assertAlmostEqual(sum(seen), 1.0, places=6)
        self.assertEqual(sum(seen[:4]), 0.0)                # already hit 70: <=65 impossible

    def test_climate_day_uses_standard_time(self):
        start, end = wm.climate_day_window("2026-10-20", -5)
        self.assertEqual(start, DAY_START)                  # midnight EST = 1 AM EDT
        self.assertEqual(end - start, 86400)
        self.assertTrue(wm.us_dst_active(NOW, -5))
        self.assertFalse(wm.us_dst_active(1767225600, -5))  # Jan 1
        st = {"std_offset": -5, "dst": True}
        self.assertEqual(wm.local_clock(NOW, st).hour, 17)

    def test_event_date(self):
        self.assertEqual(wm.event_date("KXHIGHNY-26OCT20"), "2026-10-20")
        self.assertEqual(wm.event_date("KXLOWTSATX-26OCT16"), "2026-10-16")
        self.assertIsNone(wm.event_date("KXRAIN"))

    def test_window_extreme_needs_coverage(self):
        times = [DAY_START + i * 3600 for i in range(24)]
        vals = [60 + (i % 12) for i in range(24)]
        self.assertEqual(wm.window_extreme(times, vals, DAY_START, DAY_START + 86400, "high"), 71)
        self.assertIsNone(wm.window_extreme(times[:10], vals[:10], DAY_START, DAY_START + 86400, "high"))


class TestParsers(unittest.TestCase):
    def test_open_meteo_members(self):
        data = {"hourly": {"time": [1, 2], "temperature_2m": [50, 51], "temperature_2m_member01": [49, 52],
                           "temperature_2m_member02": [48, 53]}, "hourly_units": {"temperature_2m": "°F"}}
        times, members = wd.parse_hourly_members(data)
        self.assertEqual(times, [1, 2])
        self.assertEqual(len(members), 3)

    def test_nws_observations_convert_celsius(self):
        data = {"features": [
            {"properties": {"timestamp": iso(DAY_START + 3600), "temperature": {"unitCode": "wmoUnit:degC", "value": 20.0}}},
            {"properties": {"timestamp": iso(DAY_START + 7200), "temperature": {"unitCode": "wmoUnit:degC", "value": None}}},
            {"properties": {"timestamp": iso(DAY_START - 3600), "temperature": {"unitCode": "wmoUnit:degC", "value": 30.0}}},
        ]}
        hi, lo, n = wd.parse_nws_observations(data, DAY_START)
        self.assertEqual((round(hi, 1), n), (68.0, 1))        # the pre-window 30°C reading is ignored

    def test_iem_cli_skips_missing(self):
        out = wd.parse_iem_cli({"results": [{"valid": "2026-10-18", "high": 70, "low": "M"},
                                            {"valid": "2026-10-19", "high": "M", "low": "M"}]})
        self.assertEqual(out, {"2026-10-18": {"high": 70}})

    def test_nws_station(self):
        st = wd.parse_nws_station({"geometry": {"coordinates": [-73.97, 40.78]},
                                   "properties": {"timeZone": "America/New_York", "name": "Central Park"}})
        self.assertEqual((st["lat"], st["std_offset"], st["dst"]), (40.78, -5, True))
        self.assertIsNone(wd.parse_nws_station({"geometry": {"coordinates": [0, 0]},
                                                "properties": {"timeZone": "Europe/Paris"}}))

    def test_station_code_and_kind(self):
        api = FakeKalshi()
        m = wx_market(api, "B64.5", "between", 0.25, 0.27, floor=64, cap=65)
        self.assertEqual(wb.station_code(m), "NYC")
        self.assertEqual(wb.market_kind(m), "high")
        self.assertTrue(wb.is_weather_market(m))
        low = wx_market(api, "T50", "less", 0.1, 0.12, cap=50, event="KXLOWTNYC-26OCT20", kind="minimum")
        self.assertEqual(wb.market_kind(low), "low")


class TestFillRules(unittest.TestCase):
    def order(self, side, price):
        return {"side": side, "price": price}

    def test_no_bid_fills_when_yes_buyers_trade(self):
        t = {"taker_outcome_side": "yes", "yes_price_dollars": "0.0300", "no_price_dollars": "0.9700", "count_fp": "40.00"}
        self.assertEqual(wb.trade_fill_qty(self.order("no", 0.98), t), 40)       # traded through us
        self.assertEqual(wb.trade_fill_qty(self.order("no", 0.97), t), 20)       # at our price: half
        self.assertEqual(wb.trade_fill_qty(self.order("no", 0.96), t), 0)        # we were too low

    def test_wrong_taker_side_never_fills(self):
        t = {"taker_outcome_side": "no", "yes_price_dollars": "0.0300", "no_price_dollars": "0.9700", "count_fp": "40.00"}
        self.assertEqual(wb.trade_fill_qty(self.order("no", 0.98), t), 0)
        self.assertEqual(wb.trade_fill_qty(self.order("yes", 0.05), t), 40)


# ---------------------------------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sdir = os.path.join(self.dir, "weather")
        self.api = FakeKalshi()
        self._saved = {}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(wc, k, v)
        shutil.rmtree(self.dir, ignore_errors=True)

    def cfg(self, **kw):
        for k, v in kw.items():
            self._saved.setdefault(k, getattr(wc, k))
            setattr(wc, k, v)

    def cycle(self, now, data=None):
        ok = wb.run_cycle(self.api, data or FakeData(), now, self.sdir, verbose=False)
        return ok, portfolio.load_json(os.path.join(self.sdir, "portfolio.json"), None)

    def assert_books_balance(self, st):
        realized = sum(p["pnl"] for p in st["closed"])
        self.assertAlmostEqual(wb.book_equity(st), wc.STARTING_BANKROLL + realized, places=3)
        self.assertGreaterEqual(st["cash"], -1e-6)


class TestWeatherCycle(Base):
    def test_longshot_no_orders_evening_before(self):
        ladder(self.api)
        ok, st = self.cycle(NOW)
        self.assertTrue(ok, st["last_run"]["error"])
        orders = {o["ticker"].split("-")[-1]: o for o in st["orders"]}
        longshots = {k for k, o in orders.items() if o["strategy"] == "longshot"}
        self.assertEqual(longshots, {"T60", "B60.5", "T71"})            # 1-4c brackets the model also rules out
        for k in longshots:
            self.assertEqual(orders[k]["side"], "no")
            self.assertLessEqual(orders[k]["price"], wc.LONGSHOT["max_no_price"])
        self.assertNotIn("B70.5", longshots)                             # yes bid 5c: outside the band
        event_total = sum(o["reserved"] for o in st["orders"] if o["event_ticker"] == "KXHIGHNY-26OCT20")
        self.assertLessEqual(event_total, wc.MAX_EVENT_FRACTION * 100 + 1e-6)
        self.assert_books_balance(st)
        self.assertEqual(len(st["last_run"]["events"]), 1)

    def test_no_longshots_outside_evening_window(self):
        ladder(self.api)
        _, st = self.cycle(NOW - 8 * H)                                   # 9 AM the day before
        self.assertFalse([o for o in st["orders"] if o["strategy"] == "longshot"])

    def test_fill_settle_and_profit(self):
        ladder(self.api)
        _, st = self.cycle(NOW)
        o = next(o for o in st["orders"] if o["ticker"].endswith("T60"))
        # a YES buyer lifts the 98c NO bid (= YES at 2c) after we posted: at our price, half fills
        self.api.add_trade(o["ticker"], NOW + 300, "yes", 0.02, 2 * o["contracts"])
        _, st = self.cycle(NOW + 900)
        pos = next(p for p in st["positions"] if p["ticker"] == o["ticker"])
        self.assertEqual(pos["contracts"], o["contracts"])
        self.assertAlmostEqual(pos["avg_price"], o["price"])
        self.assertFalse([x for x in st["orders"] if x["ticker"] == o["ticker"]])
        self.assert_books_balance(st)
        self.api.settle(o["ticker"], "no")                                 # high was not <60: NO wins
        _, st = self.cycle(CLOSE + 6 * H)
        won = next(p for p in st["closed"] if p["ticker"] == o["ticker"])
        self.assertTrue(won["won"])
        self.assertAlmostEqual(won["pnl"], round(won["contracts"] * (1 - won["avg_price"]), 4), places=3)
        self.assertEqual(st["orders"], [])                                 # everything else cancelled at close
        self.assert_books_balance(st)

    def test_trades_before_order_or_wrong_side_dont_fill(self):
        ladder(self.api)
        _, st = self.cycle(NOW)
        o = next(o for o in st["orders"] if o["ticker"].endswith("T60"))
        self.api.add_trade(o["ticker"], NOW - 600, "yes", 0.01, 500)       # before we posted
        self.api.add_trade(o["ticker"], NOW + 300, "no", 0.01, 500)        # wrong side
        _, st = self.cycle(NOW + 900)
        self.assertFalse(st["positions"])

    def test_reprice_when_book_moves(self):
        ladder(self.api)
        _, st = self.cycle(NOW)
        t = next(o["ticker"] for o in st["orders"] if o["ticker"].endswith("B60.5"))
        before = next(o for o in st["orders"] if o["ticker"] == t)
        self.api.set_quote(t, 0.01, 0.03)                                  # NO side now 97/99: room to improve
        _, st = self.cycle(NOW + 900)
        after = next(o for o in st["orders"] if o["ticker"] == t)
        self.assertNotEqual(after["price"], before["price"])
        self.assertTrue(any("reprice" in c["reason"] for c in st["last_run"]["cancelled"]))
        self.assert_books_balance(st)

    def test_goal_lock_cancels_everything(self):
        ladder(self.api)
        self.cycle(NOW)
        st = portfolio.load_json(os.path.join(self.sdir, "portfolio.json"), None)
        st["cash"] += 60                                                   # pretend we won big
        portfolio.save_json(os.path.join(self.sdir, "portfolio.json"), st)
        _, st = self.cycle(NOW + 900)
        self.assertEqual(st["orders"], [])
        self.assertIn("Goal reached", st["last_run"]["halt"])
        self.assertIn("goal_reached_ts", st)

    def test_stop_posting_near_close(self):
        ladder(self.api)
        _, st = self.cycle(CLOSE - 30 * 60)
        self.assertEqual(st["orders"], [])

    def test_edge_and_favorite_strategies(self):
        # mispriced: 64-65 at 9-11c though the model says ~30%; >=63 favorite at 86/88c vs ~95%
        wx_market(self.api, "B64.5", "between", 0.09, 0.11, floor=64, cap=65)
        wx_market(self.api, "T62", "greater", 0.86, 0.88, floor=62)
        # fairly priced bracket: no lowball bid behind the book
        wx_market(self.api, "B66.5", "between", 0.38, 0.40, floor=66, cap=67)
        _, st = self.cycle(NOW)
        strat = {o["ticker"].split("-")[-1]: (o["strategy"], o["side"]) for o in st["orders"]}
        self.assertEqual(strat.get("B64.5"), ("edge", "yes"))
        self.assertEqual(strat.get("T62"), ("favorite", "yes"))
        self.assertNotIn("B66.5", strat)
        for o in st["orders"]:                       # every order sits at or above the best bid
            bid = wb.fav.side_quote(self.api.markets[o["ticker"]], o["side"])["bid"] or 0
            self.assertGreaterEqual(o["price"], bid - 1e-9)
        self.assert_books_balance(st)

    def test_observations_kill_impossible_brackets(self):
        wx_market(self.api, "B64.5", "between", 0.09, 0.11, floor=64, cap=65)
        _, st = self.cycle(DAY_START + 15 * H, FakeData(observed=70.0))   # 3 PM ET on the day, already 70°
        self.assertFalse([o for o in st["orders"] if o["side"] == "yes"])
        ev = st["last_run"]["events"][0]
        self.assertEqual(ev["observed"], 70.0)
        self.assertEqual(ev["rows"][0]["q"], 0.0)

    def test_data_failure_is_reported_not_fatal(self):
        class Broken(FakeData):
            def forecast_extremes(self, code, st):
                raise OSError("open-meteo down")
        ladder(self.api)
        ok, st = self.cycle(NOW, Broken())
        self.assertFalse(ok)
        self.assertIn("open-meteo down", st["last_run"]["error"])
        self.assert_books_balance(st)

    def test_scorecard_grades_model_vs_market(self):
        ladder(self.api)
        self.cycle(NOW)
        for t in list(self.api.markets):
            self.api.settle(t, "yes" if t.endswith("B66.5") else "no")
        _, st = self.cycle(CLOSE + 6 * H)
        tot = st["scorecard"]["totals"]["all"]
        self.assertEqual(tot["n"], 8)
        self.assertGreater(tot["market"], 0)


class TestDashboard(Base):
    def test_tabs_render_both_bots(self):
        ladder(self.api)
        self.cycle(NOW)
        wst = portfolio.load_json(os.path.join(self.sdir, "portfolio.json"), None)
        fav = portfolio.new_state(NOW)
        page = report.build_html(fav, {"entries": {}}, NOW, wst)
        for s in ("Favorites bot", "Weather bot", "Longshot NO", "Is the forecast beating the market?",
                  "Forecast board", "Goal: $150.00", "tab-wx", "Is the edge real?"):
            self.assertIn(s, page)

    def test_weather_tab_before_first_run(self):
        page = report.build_html(portfolio.new_state(NOW), {"entries": {}}, NOW, None)
        self.assertIn("Hasn't run yet", page)

    def test_run_all_keeps_favorites_alive_when_weather_crashes(self):
        import weather_bot
        orig = weather_bot.run_cycle
        weather_bot.run_cycle = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        old = (paper_bot.config.STATE_DIR, paper_bot.config.REPORT_PATH, wc.STATE_DIR)
        real = [os.path.abspath(p) for p in old]
        try:
            paper_bot.config.STATE_DIR = os.path.join(self.dir, "state")
            paper_bot.config.REPORT_PATH = os.path.join(self.dir, "docs", "index.html")
            wc.STATE_DIR = os.path.join(self.dir, "state", "weather")
            self.api.add("KXA-26OCT20-A", NOW + 5 * H, 0.89, 0.90)
            before = {p: os.path.getmtime(p) for p in real if os.path.exists(p)}
            ok = paper_bot.run_all(self.api, NOW)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(paper_bot.config.REPORT_PATH))
            fav = portfolio.load_json(os.path.join(self.dir, "state", "portfolio.json"), None)
            self.assertEqual(len(fav["open_positions"]), 1)                  # bet landed in the TEMP account
            for p, t in before.items():                                      # ...and nothing real was touched
                self.assertEqual(os.path.getmtime(p), t, f"test modified real file {p}")
        finally:
            weather_bot.run_cycle = orig
            paper_bot.config.STATE_DIR, paper_bot.config.REPORT_PATH, wc.STATE_DIR = old


if __name__ == "__main__":
    unittest.main()


class TestWeatherDataLayer(unittest.TestCase):
    """Exercises weather_data.WeatherData end to end with canned HTTP responses."""
    def setUp(self):
        self.calls = []
        self.down = set()
        self.orig = wd.http_json
        hours = [NOW - 30 * H + i * H for i in range(100)]

        def fake(url, params=None, retries=2):
            self.calls.append(url)
            for k in self.down:
                if k in url:
                    raise OSError(f"{k} unreachable")
            if url.endswith("/stations/KNYC"):
                return {"geometry": {"coordinates": [-73.97, 40.78]},
                        "properties": {"timeZone": "America/New_York", "name": "Central Park"}}
            if "ensemble" in url:
                n = 51 if params["models"] == "ecmwf_ifs025" else 31
                hourly = {"time": hours}
                for i in range(n):
                    key = "temperature_2m" if i == 0 else f"temperature_2m_member{i:02d}"
                    hourly[key] = [60 + 6 * math.sin((t / 3600 - 11) / 24 * 2 * math.pi) + (i % 5) * 0.3 for t in hours]
                return {"hourly": hourly}
            if "historical-forecast" in url:
                hs = [DAY_START - 40 * 86400 + i * H for i in range(40 * 24)]
                return {"hourly": {"time": hs, "temperature_2m": [62 + 5 * math.sin((t / 3600 - 11) / 24 * 2 * math.pi) for t in hs]}}
            if "api.open-meteo.com" in url:
                return {"hourly": {"time": hours, "temperature_2m": [61.0] * len(hours)}}
            if "cli.py" in url:
                res = []
                for i in range(1, 40):
                    d = datetime_str(DAY_START - i * 86400)
                    res.append({"valid": d, "high": 68, "low": 57})
                return {"results": res}
            if "/observations" in url:
                return {"features": [{"properties": {"timestamp": iso(DAY_START + 3600),
                                                     "temperature": {"unitCode": "wmoUnit:degC", "value": 18.0}}}]}
            raise AssertionError("unexpected url " + url)
        wd.http_json = fake

    def tearDown(self):
        wd.http_json = self.orig

    def test_full_fetch_cache_and_fallbacks(self):
        cache = {}
        data = wd.WeatherData(cache, NOW)
        st = data.station("NYC")
        self.assertEqual((st["src"], st["std_offset"]), ("nws", -5))
        days = data.forecast_extremes("NYC", st)
        self.assertIn("2026-10-20", days)
        self.assertEqual(len(days["2026-10-20"]["high"]["ecmwf"]), 51)
        self.assertEqual(len(days["2026-10-20"]["low"]["gfs"]), 31)
        nbm = data.nbm_extremes("NYC", st)
        self.assertEqual(nbm["2026-10-20"]["high"], 61.0)
        cal = data.calibration("NYC", st)
        self.assertGreater(cal["high"]["bias"], 0)             # actual 68 vs forecast ~67
        self.assertGreaterEqual(cal["high"]["n"], 20)
        self.assertEqual(data.observed_so_far("NYC", DAY_START), (None, None))   # day not started yet
        hi, lo = data.observed_so_far("NYC", NOW - 5 * H)
        self.assertAlmostEqual(hi, 64.4, places=1)
        # second pass inside the refresh window: served from cache, no new HTTP
        n = len(self.calls)
        data2 = wd.WeatherData(cache, NOW + 600)
        data2.forecast_extremes("NYC", data2.station("NYC"))
        data2.nbm_extremes("NYC", st)
        self.assertEqual(len(self.calls), n)
        # later, with the forecast API down: stale forecast is reused, failure is recorded
        self.down = {"ensemble"}
        data3 = wd.WeatherData(cache, NOW + 3 * H)
        self.assertIn("2026-10-20", data3.forecast_extremes("NYC", st))
        self.assertEqual(data3.health["open_meteo_ensemble"]["fail"], 2)

    def test_station_falls_back_when_nws_down(self):
        self.down = {"api.weather.gov"}
        data = wd.WeatherData({}, NOW)
        st = data.station("MDW")
        self.assertEqual((st["src"], st["std_offset"]), ("fallback", -6))
        self.assertIsNone(data.station("ZZZ"))

    def test_bot_runs_on_real_data_layer(self):
        api = FakeKalshi()
        ladder(api)
        d = tempfile.mkdtemp()
        try:
            ok = wb.run_cycle(api, None, NOW, d, verbose=False)
            st = portfolio.load_json(os.path.join(d, "portfolio.json"), None)
            self.assertTrue(ok, st["last_run"]["error"])
            self.assertEqual(len(st["last_run"]["events"]), 1)
            self.assertTrue(os.path.exists(os.path.join(d, "cache.json")))
        finally:
            shutil.rmtree(d, ignore_errors=True)


def datetime_str(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
