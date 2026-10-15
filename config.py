"""
Every knob the bot uses lives here. Tune these, not the code.

Money is in dollars. Prices are in dollars per contract (0.90 = 90 cents).
A contract pays $1.00 if your side wins and $0.00 if it loses.
"""

# --- Paper account ----------------------------------------------------------
STARTING_BANKROLL = 100.00

# --- Which markets the bot will even look at -------------------------------
MIN_HOURS_TO_CLOSE = 0.25      # skip markets about to close (quotes go stale, can't fill)
MAX_HOURS_TO_CLOSE = 36        # short-dated only, so bets resolve inside the test week
MIN_VOLUME_24H = 200           # contracts traded in the last 24h (proves a real market)
MAX_SPREAD = 0.03              # ask minus bid on the side we'd buy; wide = untrustworthy price
EXCLUDE_TICKER_PREFIXES = [    # series to ignore entirely
    "KXMVE",                   # multi-leg combos / parlays
]

# --- Experiment label ---------------------------------------------------------
# Change this whenever you change the strategy. The dashboard then tracks
# results since the change separately, so each experiment gets a clean score.
PHASE_NAME = "Week 2: 6% bets, 85-95¢ only"

# --- What counts as a "good bet" --------------------------------------------
MIN_PRICE = 0.80               # markets 80-95c are TRACKED for the evidence table...
BET_MIN_PRICE = 0.85           # ...but only 85c+ are BET. Week 1: 80-85c favorites won 77%
                               # vs 82.5% needed (-6.4c per $ staked), the worst band.
MAX_PRICE = 0.95               # ...but not so expensive that fees + one loss wipe out the gain
MAX_ALL_IN_COST = 0.965        # price + fee per contract; guarantees a win pays >= ~3.5 cents
MAX_DAILY_DROP = 0.06          # skip a favorite whose price fell > 6 cents vs a day ago

# --- Risk management ----------------------------------------------------------
# Week 2 ramp-up. Week 1 (3% bets, 60% max in play) sat on ~$68 of idle cash and
# skipped qualified markets because all 15 slots were full. Simulated week at these
# settings: about +8.5% if last week's results reflect a real edge, about -3% if
# prices are simply fair; roughly 1 week in 11 ends down 20%+ in the no-edge case.
RISK_PER_TRADE = 0.06          # max 6% of the account on one bet (~$0.45 win / ~$6.40 loss)
MAX_TOTAL_EXPOSURE = 0.90      # keep at least 10% of the account in cash
MAX_OPEN_POSITIONS = 20
MAX_POSITIONS_PER_SERIES = 2   # e.g. no more than 2 bets on the same weather city / league
MAX_NEW_TRADES_PER_RUN = 5     # spread entries out over time
DAILY_STOP_LOSS = 0.15         # stop opening bets for the rest of the UTC day if down 15% today
MAX_DRAWDOWN_HALT = 0.35       # stop opening bets entirely if 35% below the account's peak

# --- Related-market cap --------------------------------------------------------
# Kalshi files closely linked markets under different series (CPI, Core CPI and
# CPI year-over-year are three series, but ONE report decides all three). Any
# market whose title matches a group below counts toward that group's cap.
# Patterns are regular expressions matched against the lowercase market title.
MAX_POSITIONS_PER_GROUP = 1
RELATED_GROUPS = {
    "Inflation reports": r"\b(cpi|pce|inflation)\b",
    "Jobs reports": r"\b(payrolls?|unemployment|jobless|jobs report)\b",
    "Fed / interest rates": r"\b(fed|fomc|federal reserve|fed funds|interest rates?)\b",
    "Oil prices": r"\b(crude|wti|brent)\b",
    "Gas prices": r"\bgas prices?\b",
    "Precious metals": r"\b(gold|silver|platinum)\b",
    "Crypto": r"\b(bitcoin|btc|ethereum|eth|solana|xrp|dogecoin|crypto)\b",
    "Stock indexes": r"(s&p|\bnasdaq\b|\bdow\b|\bspx\b)",
    "Presidential approval": r"\bapproval (average|rating)\b",
    "Strait of Hormuz": r"\bhormuz\b",
}

# --- Shadow stop-loss (logging only, never trades) ---------------------------
# For each bet, record the first time its bid falls this far below what we paid,
# and what selling right then would have returned. The dashboard compares that
# with what holding to settlement actually did. Several thresholds are tested
# side by side so one week answers "would a stop-loss help, and at what size?"
SHADOW_STOP_DROPS = [0.10, 0.15, 0.20, 0.30]

# --- Fees (Kalshi general taker schedule) -----------------------------------
# fee per order = round_up_to_cent(0.07 * contracts * price * (1 - price))
TAKER_FEE_RATE = 0.07

# --- Evidence tracking ------------------------------------------------------
# Every market that passes the quality filters is logged (bet or not) and graded
# when it settles. That tells you if favorites are actually beating their prices.
WATCHLIST_MAX_ADDS_PER_RUN = 150
WATCHLIST_MAX_CHECKS_PER_RUN = 300

# --- Plumbing ---------------------------------------------------------------
API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
STATE_DIR = "state"
REPORT_PATH = "docs/index.html"
REQUEST_PAUSE_SEC = 0.15       # be polite to the public API
DISPLAY_TIMEZONE = "America/Chicago"
