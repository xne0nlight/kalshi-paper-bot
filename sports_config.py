"""
Settings for the SPORTS PARLAY bot (paper money).

Two $100 accounts side by side, betting the SAME parlays:
  * rolling - builds each parlay from single-game markets: bet leg 1 with a limit
              order; if it wins, the whole payout rides on leg 2, and so on.
              Same payout as a parlay, but each leg pays single-market prices.
  * combo   - what Kalshi's built-in parlays ("combos") would have done, priced
              with the ~15% cut combo bettors have historically paid.
"""
import os

PHASE_NAME = "Sports v1: rolling vs combo"
STARTING_BANKROLL = 100.00          # each account
FLOOR_EQUITY = 30.00                # stop starting new parlays below this

# --- Leagues: Kalshi game-winner series -> Odds API sport key / ESPN path / game length
LEAGUES = {
    "NFL":   {"series": "KXNFLGAME",   "odds": "americanfootball_nfl",   "espn": "football/nfl",
              "hours": 3.4},
    "NCAAF": {"series": "KXNCAAFGAME", "odds": "americanfootball_ncaaf", "espn": "football/college-football",
              "hours": 3.6},
    "NBA":   {"series": "KXNBAGAME",   "odds": "basketball_nba",         "espn": "basketball/nba",
              "hours": 2.6},
    "NHL":   {"series": "KXNHLGAME",   "odds": "icehockey_nhl",          "espn": "hockey/nhl",
              "hours": 2.8},
}
START_FROM_CLOSE_HOURS = 48         # Kalshi game markets close 48h after scheduled start

# --- Sportsbook odds (The Odds API, free plan = 500 credits/month) -------------------
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
# Up to 10 bookmakers cost 1 credit per sport per request. Sharp books get extra weight.
BOOKMAKERS = ["pinnacle", "betfair_ex_eu", "matchbook", "lowvig", "betonlineag",
              "draftkings", "fanduel", "betmgm", "williamhill_us", "bovada"]
SHARP_BOOKS = {"pinnacle": 3.0, "betfair_ex_eu": 2.0, "matchbook": 2.0, "lowvig": 1.5, "betonlineag": 1.5}
MIN_BOOKS = 2
ODDS_REFRESH_HOURS = 4              # per sport, only when Kalshi has games coming up
ODDS_MAX_AGE_HOURS = 6              # older than this: don't start new parlays on it
MONTHLY_CREDITS = 500
CREDIT_RESERVE = 20                 # never spend the last few credits of the month

# --- Which legs qualify ----------------------------------------------------------------
LEG_PROB_RANGE = (0.35, 0.85)       # no huge longshots, no tiny-payout heavy favorites
LEG_MIN_EDGE = 0.02                 # fair prob must beat our price by 2+ points
LEG_MAX_DISAGREEMENT = 0.15         # books and Kalshi >15 pts apart = probably breaking news: skip
LEG_MAX_SPREAD = 0.05               # Kalshi bid/ask must be reasonably tight
FAIR_WEIGHT_FOR_SIZING = 0.5        # shrink the books' probability 50% toward Kalshi's price

# --- Parlay shape ------------------------------------------------------------------------
MIN_LEGS, MAX_LEGS = 2, 4
PARLAY_PRICE_RANGE = (0.07, 0.40)   # pays about 2.5x to 14x
SETTLE_BUFFER_HOURS = 0.75          # after a game ends: settlement + time to place the next leg
HORIZON_HOURS = 40                  # legs must start within this window
FIRST_LEG_WITHIN_HOURS = 8          # only create a parlay when leg 1 starts soon
MAX_ACTIVE_PARLAYS = 4
MAX_NEW_PARLAYS_PER_DAY = 3

# --- Staking -----------------------------------------------------------------------------
KELLY_FRACTION = 0.25               # quarter-Kelly: parlays are very streaky
MAX_STAKE_FRACTION = 0.04           # never more than 4% of an account on one parlay
MIN_STAKE = 1.00

# --- Order handling (rolling account) -------------------------------------------------------
LEG_CUTOFF_MINUTES = 10             # unfilled this close to tip-off: take the ask if still +EV, else stop
TAKER_MIN_EDGE = 0.01               # taker fallback must still beat price+fee by 1 point
FILL_AT_PRICE_SHARE = 0.5
MAKER_FEE_RATE = 0.0175             # some sports series charge makers; assume they all do (conservative)
TAKER_FEE_RATE = 0.07

# --- Combo account ---------------------------------------------------------------------------
COMBO_HOLD = 0.15                   # Jan-Apr 2026: combo bettors lost ~15% of what they risked

# --- Plumbing ----------------------------------------------------------------------------------
STATE_DIR = "state/sports"
HTTP_TIMEOUT = 20
USER_AGENT = "kalshi-paper-bot/1.0 (github.com/xne0nlight)"
