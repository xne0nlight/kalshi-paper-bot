"""Offline tests for the sports parlay bot. Run: python -m unittest discover tests -v"""
import math
import os
import shutil
import tempfile
import unittest

from fake_kalshi import FakeKalshi, iso

import paper_bot
import portfolio
import report
import sports_bot as sb
import sports_config as sc
import sports_data as sd
import weather_config as wc

# Sunday Oct 25 2026, 10:00 AM Central (15:00 UTC)
NOW = 1792940400
H = 3600
GBDET = 1792959900      # 20:25 UTC Sun (3:25 PM CT)
KCSEA = 1792974000      # 00:20 UTC Mon (Sunday night game)
DALPHI = 1793060100     # 00:15 UTC Tue (Monday night game)
NENYJ = 1792947600      # 17:00 UTC Sun (noon CT)


def game(api, league_series, date, home_abbr, away_abbr, home, away, start, home_q, away_q):
    """Two team markets for one game, priced like Kalshi (yes bid/ask per team)."""
    ev = f"{league_series}-{date}{away_abbr}{home_abbr}"
    close = start + sc.START_FROM_CLOSE_HOURS * H
    for abbr, team, (bid, ask) in ((home_abbr, home, home_q), (away_abbr, away, away_q)):
        m = api.add(f"{ev}-{abbr}", close, bid, ask, event=ev, title=f"{team} wins", vol24=5000)
        m["yes_sub_title"] = team
        m["expected_expiration_time"] = iso(start + 3 * H)
    return ev


def nfl_sunday(api):
    game(api, "KXNFLGAME", "26OCT25", "NYJ", "NE", "New York J", "New England", NENYJ, (0.48, 0.49), (0.51, 0.52))
    game(api, "KXNFLGAME", "26OCT25", "DET", "GB", "Detroit", "Green Bay", GBDET, (0.57, 0.58), (0.42, 0.43))
    game(api, "KXNFLGAME", "26OCT25", "SEA", "KC", "Seattle", "Kansas City", KCSEA, (0.54, 0.55), (0.45, 0.46))
    game(api, "KXNFLGAME", "26OCT26", "PHI", "DAL", "Philadelphia", "Dallas", DALPHI, (0.34, 0.36), (0.64, 0.66))


class FakeSportsData:
    """Books: Detroit 62%, Kansas City 50%, Dallas 68% (Kalshi is cheaper on all three)."""
    def __init__(self, fair=None):
        self.fair = fair or {"Detroit Lions": 0.62, "Kansas City Chiefs": 0.50, "Dallas Cowboys": 0.68,
                             "New York Jets": 0.485, "New England Patriots": 0.515}
        self.health = {}
        self.now = NOW

    def _ev(self, home, away, start):
        ph = self.fair.get(home)
        pa = self.fair.get(away)
        if ph is None:
            ph = 1 - pa
        if pa is None:
            pa = 1 - ph
        return {"start_ts": start, "home": home, "away": away, "fair": {home: ph, away: pa}, "books": 6, "sharp": True}

    def odds(self, league, force=False):
        if league != "NFL":
            return [], None
        return [self._ev("New York Jets", "New England Patriots", NENYJ),
                self._ev("Detroit Lions", "Green Bay Packers", GBDET),
                self._ev("Seattle Seahawks", "Kansas City Chiefs", KCSEA),
                self._ev("Philadelphia Eagles", "Dallas Cowboys", DALPHI)], self.now - 600

    def espn(self, league, d):
        if league != "NFL":
            return []
        return [{"start_ts": GBDET, "state": "pre", "home": "Detroit Lions", "away": "Green Bay Packers",
                 "teams": {"home": {"name": "Detroit Lions", "record": "5-1"},
                           "away": {"name": "Green Bay Packers", "record": "3-3"}}}]

    def budget_info(self):
        return {"remaining": 480, "today": 2, "has_key": True}

    def prune(self):
        pass


# ---------------------------------------------------------------------------
class TestMatchingAndOdds(unittest.TestCase):
    def test_name_scores(self):
        self.assertEqual(sd.name_score("Kansas City", "Kansas City Chiefs"), 1.0)
        self.assertEqual(sd.name_score("Mississippi St.", "Mississippi State Bulldogs"), 1.0)
        self.assertEqual(sd.name_score("Los Angeles R", "Los Angeles Rams"), 1.0)
        self.assertLess(sd.name_score("Los Angeles R", "Los Angeles Chargers"), 1.0)
        self.assertEqual(sd.name_score("Detroit", "Dallas Cowboys"), 0.0)

    def test_match_pair_uses_both_teams_and_time(self):
        evs = [{"start_ts": 100, "home": "Los Angeles Chargers", "away": "Denver Broncos"},
               {"start_ts": 100, "home": "Los Angeles Rams", "away": "San Francisco 49ers"},
               {"start_ts": 100 + 86400 * 3, "home": "Los Angeles Rams", "away": "San Francisco 49ers"}]
        ev, names = sd.match_pair("San Francisco", "Los Angeles R", 100, evs)
        self.assertIs(ev, evs[1])
        self.assertEqual(names["Los Angeles R"], "Los Angeles Rams")
        self.assertEqual(sd.match_pair("Dallas", "Detroit", 100, evs), (None, None))

    def test_devig_and_consensus(self):
        p = sd.devig([1.80, 2.10])                          # 55.6% + 47.6% = 103% -> margin removed
        self.assertAlmostEqual(sum(p), 1.0)
        ev = {"bookmakers": [
            {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 1.60}, {"name": "B", "price": 2.50}]}]},
            {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 1.50}, {"name": "B", "price": 2.60}]}]},
            {"key": "soccerbook", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 2}, {"name": "B", "price": 3}, {"name": "Draw", "price": 3}]}]}]}
        f = sd.fair_from_event(ev)
        self.assertEqual(f["books"], 2)                      # 3-way book ignored
        self.assertTrue(f["sharp"])
        self.assertAlmostEqual(f["fair"]["A"] + f["fair"]["B"], 1.0, places=3)
        pin = sd.devig([1.60, 2.50])[0]
        dk = sd.devig([1.50, 2.60])[0]
        self.assertAlmostEqual(f["fair"]["A"], round((3 * pin + dk) / 4, 4), places=4)   # Pinnacle weighted 3x

    def test_credit_rationing(self):
        cache = {}
        d = sd.SportsData(cache, NOW)
        self.assertTrue(d.credits_ok())                      # unknown balance: allowed to ask once
        cache["credits"].update(remaining=sc.CREDIT_RESERVE + 14, by_day={d._today(): 1})
        self.assertTrue(d.credits_ok())                      # 14 spare over 7 days left -> 2/day; 1 used
        cache["credits"]["by_day"][d._today()] = 2
        self.assertFalse(d.credits_ok())                     # today's share is spent
        cache["credits"]["remaining"] = sc.CREDIT_RESERVE
        self.assertFalse(d.credits_ok())                     # reserve is untouchable

    def test_espn_parser(self):
        data = {"events": [{"date": "2026-10-25T20:25Z", "status": {"type": {"state": "pre", "shortDetail": "3:25 PM"}},
                            "competitions": [{"competitors": [
                                {"homeAway": "home", "team": {"displayName": "Detroit Lions"}, "records": [{"summary": "5-1"}]},
                                {"homeAway": "away", "team": {"displayName": "Green Bay Packers"}, "records": [{"summary": "3-3"}]}]}]}]}
        g = sd.parse_espn(data)[0]
        self.assertEqual((g["home"], g["state"], g["start_ts"]), ("Detroit Lions", "pre", GBDET))
        self.assertEqual(g["teams"]["away"]["record"], "3-3")


# ---------------------------------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sdir = os.path.join(self.dir, "sports")
        self.api = FakeKalshi()
        self.data = FakeSportsData()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def cycle(self, now):
        self.data.now = now
        ok = sb.run_cycle(self.api, self.data, now, self.sdir, verbose=False)
        st = portfolio.load_json(os.path.join(self.sdir, "portfolio.json"), None)
        self.assertTrue(ok, st["last_run"]["error"])
        return st

    def assert_balanced(self, st):
        """Money is conserved: nothing created or lost outside of bets."""
        ps = st["parlays"]
        closed_payouts = sum(p["payout"] for p in ps if p["payout"] is not None)
        self.assertAlmostEqual(st["rolling"]["cash"], 100 - sum(p["stake"] for p in ps) + closed_payouts, places=3)
        for p in ps:
            self.assertGreaterEqual(p["chain_cash"], -1e-6)
            if p["status"] == "active":
                spent = sum(l["cost"] for l in p["legs"])
                won = sum(l["contracts"] * l["result"] for l in p["legs"] if l["status"] == "won")
                held = sum((l["order"] or {}).get("reserved", 0) for l in p["legs"])
                self.assertAlmostEqual(p["chain_cash"] + held, p["stake"] - spent + won, places=3)
        combo_payouts = sum(p["combo"]["payout"] or 0 for p in ps)
        self.assertAlmostEqual(st["combo"]["cash"], 100 - sum(p["combo"]["stake"] for p in ps) + combo_payouts, places=3)
        if all(p["status"] != "active" and p["combo"]["status"] != "active" for p in ps):
            self.assertAlmostEqual(sb.rolling_equity(st), 100 + sum(p["pnl"] or 0 for p in ps), places=3)

    def fill(self, st, team_suffix, at):
        p = st["parlays"][0]
        leg = next(l for l in p["legs"] if l["ticker"].endswith(team_suffix))
        o = leg["order"]
        self.api.add_trade(leg["ticker"], at, "no", o["price"], 3 * o["contracts"])   # sellers hit our bid
        return leg


class TestRollingParlay(Base):
    def test_builds_timing_safe_chain(self):
        nfl_sunday(self.api)
        st = self.cycle(NOW)
        self.assertEqual(len(st["parlays"]), 1)
        p = st["parlays"][0]
        teams = [l["team"] for l in p["legs"]]
        # Detroit (3:25) can't roll into the Sunday-night game (it isn't over yet), so: Detroit -> Dallas (Monday)
        self.assertEqual(teams, ["Detroit", "Dallas"])
        self.assertEqual(p["legs"][0]["status"], "ordered")
        self.assertEqual(p["legs"][1]["status"], "planned")
        self.assertAlmostEqual(p["legs"][0]["order"]["price"], 0.57)
        self.assertLessEqual(p["stake"], 100 * sc.MAX_STAKE_FRACTION + 1e-9)
        self.assertAlmostEqual(p["combo"]["price"], round(0.575 * 0.65 / (1 - sc.COMBO_HOLD), 4))
        self.assert_balanced(st)
        board = {b["team"]: b for b in st["last_run"]["board"]}
        self.assertEqual(board["Detroit"]["record"], "5-1")
        self.assertFalse(board["New York J"]["ok"])           # no edge on the noon game

    def test_full_win_both_accounts(self):
        nfl_sunday(self.api)
        st = self.cycle(NOW)
        stake = st["parlays"][0]["stake"]
        self.fill(st, "-DET", NOW + 600)
        st = self.cycle(NOW + 900)
        self.assertEqual(st["parlays"][0]["legs"][0]["status"], "filled")
        self.api.settle("KXNFLGAME-26OCT25GBDET-DET", "yes")
        st = self.cycle(GBDET + 4 * H)
        p = st["parlays"][0]
        self.assertEqual(p["legs"][0]["status"], "won")
        self.assertEqual(p["legs"][1]["status"], "ordered")    # winnings rolled into Dallas
        self.assertGreater(p["legs"][1]["order"]["reserved"], stake * 1.5)
        self.assert_balanced(st)
        self.fill(st, "-DAL", GBDET + 5 * H)
        st = self.cycle(GBDET + 6 * H)
        self.api.settle("KXNFLGAME-26OCT26DALPHI-DAL", "yes")
        st = self.cycle(DALPHI + 4 * H)
        p = st["parlays"][0]
        self.assertEqual(p["status"], "won")
        self.assertGreater(p["payout"], stake * 2.4)            # ~1/(0.57*0.65) minus fees
        self.assertEqual(p["combo"]["status"], "won")
        self.assertGreater(p["pnl"] / p["stake"], p["combo"]["pnl"] / p["combo"]["stake"])   # rolling beat the combo cut
        self.assert_balanced(st)

    def test_second_leg_loses(self):
        nfl_sunday(self.api)
        st = self.cycle(NOW)
        self.fill(st, "-DET", NOW + 600)
        self.cycle(NOW + 900)
        self.api.settle("KXNFLGAME-26OCT25GBDET-DET", "yes")
        st = self.cycle(GBDET + 4 * H)
        self.fill(st, "-DAL", GBDET + 5 * H)
        self.cycle(GBDET + 6 * H)
        self.api.settle("KXNFLGAME-26OCT26DALPHI-DAL", "no")
        st = self.cycle(DALPHI + 4 * H)
        p = st["parlays"][0]
        self.assertEqual((p["status"], p["combo"]["status"]), ("lost", "lost"))
        self.assertLess(p["pnl"], 0)
        self.assertGreaterEqual(p["pnl"], -p["stake"] - 1e-9)
        self.assert_balanced(st)

    def test_combo_loses_immediately_when_leg_one_loses(self):
        nfl_sunday(self.api)
        st = self.cycle(NOW)
        self.fill(st, "-DET", NOW + 600)
        self.cycle(NOW + 900)
        self.api.settle("KXNFLGAME-26OCT25GBDET-DET", "no")
        st = self.cycle(GBDET + 4 * H)
        p = st["parlays"][0]
        self.assertEqual((p["status"], p["combo"]["status"]), ("lost", "lost"))
        self.assert_balanced(st)

    def test_unfilled_first_leg_is_void_and_refunded(self):
        nfl_sunday(self.api)
        self.cycle(NOW)
        # nobody trades at our price; at kickoff Detroit's ask (58c) + fee leaves < 1 pt of edge? 0.62-0.58-0.017 = 2.3pts
        # -> taker fallback is still worth it, so make the ask expensive first
        self.api.set_quote("KXNFLGAME-26OCT25GBDET-DET", 0.60, 0.61)
        st = self.cycle(GBDET - 5 * 60)
        p = st["parlays"][0]
        self.assertEqual(p["status"], "void")
        self.assertAlmostEqual(p["pnl"], 0.0, places=4)
        self.assert_balanced(st)

    def test_taker_fallback_when_still_cheap(self):
        nfl_sunday(self.api)
        self.cycle(NOW)
        st = self.cycle(GBDET - 5 * 60)            # never filled as a maker; ask 58c still +EV vs 62%
        leg = st["parlays"][0]["legs"][0]
        self.assertEqual(leg["status"], "filled")
        self.assertAlmostEqual(leg["cost"] / leg["contracts"], 0.58 + sb.taker_fee(leg["contracts"], 0.58) / leg["contracts"], places=3)
        self.assert_balanced(st)

    def test_second_leg_without_edge_banks_winnings(self):
        nfl_sunday(self.api)
        st = self.cycle(NOW)
        self.fill(st, "-DET", NOW + 600)
        self.cycle(NOW + 900)
        self.api.settle("KXNFLGAME-26OCT25GBDET-DET", "yes")
        self.api.set_quote("KXNFLGAME-26OCT26DALPHI-DAL", 0.69, 0.70)   # Dallas now pricier than the books' 68%
        st = self.cycle(GBDET + 4 * H)
        self.assertEqual(st["parlays"][0]["legs"][1]["status"], "planned")   # waits for a better price
        st = self.cycle(DALPHI - 5 * 60)
        p = st["parlays"][0]
        self.assertEqual(p["status"], "banked")
        self.assertGreater(p["pnl"], 0)                     # kept the Detroit win
        self.assertEqual(p["combo"]["status"], "active")    # the combo is locked in until Monday
        self.assert_balanced(st)

    def test_no_odds_key_no_parlays(self):
        class NoOdds(FakeSportsData):
            def odds(self, league, force=False):
                return [], None

            def budget_info(self):
                return {"remaining": None, "today": 0, "has_key": False}
        self.data = NoOdds()
        nfl_sunday(self.api)
        st = self.cycle(NOW)
        self.assertEqual(st["parlays"], [])
        self.assertEqual(st["last_run"]["eligible"], 0)


class TestDashboardAndWiring(Base):
    def test_three_tabs(self):
        nfl_sunday(self.api)
        st = self.cycle(NOW)
        page = report.build_html(portfolio.new_state(NOW), {"entries": {}}, NOW, None, st)
        for s in ("Favorites", "Weather", "Sports parlays", "Rolling parlays", "Kalshi combos (simulated)",
                  "Are the sportsbooks beating Kalshi?", "Game board", "Detroit", "tab-sp"):
            self.assertIn(s, page)

    def test_run_all_runs_three_bots_in_temp_dirs_only(self):
        old = (paper_bot.config.STATE_DIR, paper_bot.config.REPORT_PATH, wc.STATE_DIR, sc.STATE_DIR)
        real = [os.path.abspath(p) for p in old]
        before = {p: os.path.getmtime(p) for p in real if os.path.exists(p)}
        try:
            paper_bot.config.STATE_DIR = os.path.join(self.dir, "state")
            paper_bot.config.REPORT_PATH = os.path.join(self.dir, "docs", "index.html")
            wc.STATE_DIR = os.path.join(self.dir, "state", "weather")
            sc.STATE_DIR = os.path.join(self.dir, "state", "sports")
            nfl_sunday(self.api)
            ok = paper_bot.run_all(self.api, NOW)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(os.path.join(self.dir, "state", "sports", "portfolio.json")))
            self.assertTrue(os.path.exists(os.path.join(self.dir, "state", "weather", "portfolio.json")))
            with open(paper_bot.config.REPORT_PATH, encoding="utf-8") as f:
                self.assertIn("Sports parlays", f.read())
            for p, t in before.items():
                self.assertEqual(os.path.getmtime(p), t, f"test modified real file {p}")
        finally:
            paper_bot.config.STATE_DIR, paper_bot.config.REPORT_PATH, wc.STATE_DIR, sc.STATE_DIR = old


if __name__ == "__main__":
    unittest.main()
