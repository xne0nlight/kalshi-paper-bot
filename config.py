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

# --- What counts as a "good bet" --------------------------------------------
MIN_PRICE = 0.80               # only buy strong favorites...
MAX_PRICE = 0.95               # ...but not so expensive that fees + one loss wipe out the gain
MAX_ALL_IN_COST = 0.965        # price + fee per contract; guarantees a win pays >= ~3.5 cents
MAX_DAILY_DROP = 0.06          # skip a favorite whose price fell > 6 cents vs a day ago

# --- Risk management (the "conservative" part) ------------------------------
RISK_PER_TRADE = 0.03          # max 3% of the account on one bet. In simulated weeks, 5%
                               # roughly doubled the swings for only a bit more upside.
MAX_TOTAL_EXPOSURE = 0.60      # keep at least 40% of the account in cash
MAX_OPEN_POSITIONS = 15
MAX_POSITIONS_PER_SERIES = 2   # e.g. no more than 2 bets on the same weather city / league
MAX_NEW_TRADES_PER_RUN = 3     # spread entries out over time
DAILY_STOP_LOSS = 0.08         # stop opening bets for the rest of the UTC day if down 8% today
MAX_DRAWDOWN_HALT = 0.20       # stop opening bets entirely if 20% below the account's peak

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
