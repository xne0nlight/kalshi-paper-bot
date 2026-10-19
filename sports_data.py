"""
Free sports data for the parlay bot.

  The Odds API (free key, 500 credits/month) - moneylines from up to 10 sportsbooks,
      including sharp ones (Pinnacle, Betfair exchange). We remove each book's
      built-in margin ("devig") and average them into a fair win probability.
  ESPN public scoreboard (no key) - start times, live status, team records.

Everything is cached and fails soft. Odds API credits are rationed so the month's
500 never run out: each day may spend (credits left - reserve) / days left in month.
"""
import calendar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import sports_config as sc

ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


def http_get(url, params=None):
    """-> (json, headers dict)."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": sc.USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=sc.HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8")), {k.lower(): v for k, v in r.headers.items()}


def iso_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fair probabilities
# ---------------------------------------------------------------------------
def devig(prices):
    """Decimal odds for every outcome -> probabilities with the book's margin removed."""
    inv = [1 / p for p in prices if p and p > 1]
    if len(inv) != len(prices) or not inv:
        return None
    s = sum(inv)
    return [x / s for x in inv]


def fair_from_event(ev):
    """Odds API event -> {'fair': {team: prob}, 'books': n, 'sharp': bool}."""
    acc, wsum, books, sharp = {}, 0.0, 0, False
    for bk in ev.get("bookmakers") or []:
        for mk in bk.get("markets") or []:
            if mk.get("key") != "h2h":
                continue
            outs = mk.get("outcomes") or []
            if len(outs) != 2:            # skip books that list a draw
                continue
            probs = devig([o.get("price") for o in outs])
            if not probs:
                continue
            w = sc.SHARP_BOOKS.get(bk.get("key"), 1.0)
            sharp = sharp or bk.get("key") in sc.SHARP_BOOKS
            for o, p in zip(outs, probs):
                acc[o["name"]] = acc.get(o["name"], 0.0) + w * p
            wsum += w
            books += 1
    if books < sc.MIN_BOOKS:
        return None
    return {"fair": {k: round(v / wsum, 4) for k, v in acc.items()}, "books": books, "sharp": sharp}


# ---------------------------------------------------------------------------
# Team-name matching (Kalshi says "Kansas City", books say "Kansas City Chiefs")
# ---------------------------------------------------------------------------
def _tokens(name):
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"\bst\.?(?=\s|$)", "state", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return [t for t in n.split() if t]


def name_score(short, full):
    """Share of the short name's words that start a word in the full name (0..1)."""
    a, b = _tokens(short), _tokens(full)
    if not a or not b:
        return 0.0
    hits = sum(1 for t in a if any(w.startswith(t) or t.startswith(w) and len(w) >= 3 for w in b))
    return hits / len(a)


def match_pair(team_a, team_b, start_ts, events, max_hours=6):
    """Find the event whose two teams best match Kalshi's two team names.
    Returns (event, {team_a: event_name_a, team_b: event_name_b}) or (None, None)."""
    best, best_map, best_score = None, None, 0.0
    for ev in events:
        if start_ts and ev.get("start_ts") and abs(ev["start_ts"] - start_ts) > max_hours * 3600:
            continue
        h, w = ev.get("home"), ev.get("away")
        for x, y in ((h, w), (w, h)):
            sa, sb = name_score(team_a, x), name_score(team_b, y)
            if min(sa, sb) >= 0.5 and (sa + sb) / 2 > best_score:
                best, best_map, best_score = ev, {team_a: x, team_b: y}, (sa + sb) / 2
    return (best, best_map) if best_score >= 0.75 else (None, None)


# ---------------------------------------------------------------------------
# ESPN scoreboard parser
# ---------------------------------------------------------------------------
def parse_espn(data):
    games = []
    for ev in (data or {}).get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        teams = {}
        for c in comp.get("competitors") or []:
            t = c.get("team") or {}
            rec = (c.get("records") or [{}])[0].get("summary") if c.get("records") else None
            teams[c.get("homeAway")] = {"name": t.get("displayName") or t.get("location"),
                                        "short": t.get("shortDisplayName"), "abbr": t.get("abbreviation"),
                                        "record": rec}
        if "home" not in teams or "away" not in teams:
            continue
        status = ((ev.get("status") or comp.get("status") or {}).get("type") or {})
        games.append({"start_ts": iso_ts(ev.get("date")), "state": status.get("state"),
                      "detail": status.get("shortDetail"), "home": teams["home"]["name"],
                      "away": teams["away"]["name"], "teams": teams})
    return games


# ---------------------------------------------------------------------------
# Client with caching + credit rationing
# ---------------------------------------------------------------------------
class SportsData:
    def __init__(self, cache, now_ts):
        self.cache = cache
        self.now = now_ts
        self.health = {}
        cache.setdefault("odds", {})
        cache.setdefault("espn", {})
        cache.setdefault("credits", {})

    def _ok(self, source, ok, err=None):
        h = self.health.setdefault(source, {"ok": 0, "fail": 0, "last_error": None})
        h["ok" if ok else "fail"] += 1
        if not ok:
            h["last_error"] = str(err)[:160]

    # -- credit budget ------------------------------------------------------------
    def _today(self):
        return datetime.fromtimestamp(self.now, timezone.utc).strftime("%Y-%m-%d")

    def credits_ok(self):
        cr = self.cache["credits"]
        now = datetime.fromtimestamp(self.now, timezone.utc)
        month = now.strftime("%Y-%m")
        if cr.get("month") != month:
            cr.clear()
            cr.update(month=month, by_day={})
        remaining = cr.get("remaining")
        if remaining is None:
            return True                      # first call this month tells us where we stand
        usable = remaining - sc.CREDIT_RESERVE
        if usable <= 0:
            return False
        days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
        allowance = max(1.0, usable / days_left)
        return cr["by_day"].get(self._today(), 0) < allowance

    def budget_info(self):
        cr = self.cache["credits"]
        return {"remaining": cr.get("remaining"), "today": cr.get("by_day", {}).get(self._today(), 0),
                "has_key": bool(sc.ODDS_API_KEY)}

    # -- sportsbook odds -------------------------------------------------------------
    def odds(self, league, force=False):
        """-> list of {'start_ts', 'home', 'away', 'fair', 'books', 'sharp', 'ts'} for a league."""
        sport = sc.LEAGUES[league]["odds"]
        entry = self.cache["odds"].get(sport)
        age_h = (self.now - entry["ts"]) / 3600 if entry else 1e9
        want = age_h >= sc.ODDS_REFRESH_HOURS or (force and age_h >= 1.0)
        if want and sc.ODDS_API_KEY and self.credits_ok():
            try:
                data, headers = http_get(ODDS_URL.format(sport=sport), {
                    "apiKey": sc.ODDS_API_KEY, "bookmakers": ",".join(sc.BOOKMAKERS[:10]),
                    "markets": "h2h", "oddsFormat": "decimal"})
                cr = self.cache["credits"]
                if headers.get("x-requests-remaining") is not None:
                    try:
                        cr["remaining"] = int(float(headers["x-requests-remaining"]))
                    except ValueError:
                        pass
                used = headers.get("x-requests-last")
                by_day = cr.setdefault("by_day", {})
                by_day[self._today()] = by_day.get(self._today(), 0) + (int(float(used)) if used else 1)
                events = []
                for ev in data or []:
                    f = fair_from_event(ev)
                    if f:
                        events.append({"start_ts": iso_ts(ev.get("commence_time")), "home": ev.get("home_team"),
                                       "away": ev.get("away_team"), **f})
                entry = {"ts": round(self.now), "events": events}
                self.cache["odds"][sport] = entry
                self._ok("odds_api", True)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if isinstance(e, urllib.error.HTTPError) and e.code == 401:
                    msg = "401 Unauthorized: check the ODDS_API_KEY secret"
                self._ok("odds_api", False, msg)
        if not entry:
            return [], None
        # keep only games that haven't long since finished
        events = [e for e in entry["events"] if not e.get("start_ts") or e["start_ts"] > self.now - 6 * 3600]
        return events, entry["ts"]

    # -- ESPN scoreboard ------------------------------------------------------------------
    def espn(self, league, date_yyyymmdd):
        key = f"{league}|{date_yyyymmdd}"
        entry = self.cache["espn"].get(key)
        if entry and self.now - entry["ts"] < 30 * 60:
            return entry["games"]
        try:
            params = {"dates": date_yyyymmdd}
            if league == "NCAAF":
                params.update(groups=80, limit=300)     # all FBS games, not just the top 25
            data, _ = http_get(ESPN_URL.format(path=sc.LEAGUES[league]["espn"]), params)
            games = parse_espn(data)
            self.cache["espn"][key] = {"ts": round(self.now), "games": games}
            self._ok("espn", True)
            return games
        except Exception as e:  # noqa: BLE001
            self._ok("espn", False, e)
            return entry["games"] if entry else []

    def prune(self):
        cutoff = self.now - 3 * 86400
        self.cache["espn"] = {k: v for k, v in self.cache["espn"].items() if v["ts"] > cutoff}
