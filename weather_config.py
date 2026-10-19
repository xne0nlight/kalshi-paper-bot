"""
Settings for the WEATHER bot (the favorites bot keeps using config.py).

It trades Kalshi's daily high / low temperature markets with paper money,
using only resting limit orders (maker orders), and decides with a forecast
model built from free professional weather data.
"""

# --- Paper account / experiment -------------------------------------------------
STARTING_BANKROLL = 100.00
PHASE_NAME = "Weather v1: maker-only, goal $150"
GOAL_EQUITY = 150.00     # once the account (book value) reaches this, stop betting and lock it in
FLOOR_EQUITY = 50.00     # bust guard: stop opening new bets below this

# --- Which markets --------------------------------------------------------------
# Daily high-temperature series all start with KXHIGH, daily lows with KXLOWT.
SERIES_KINDS = {"KXHIGH": "high", "KXLOWT": "low"}
MAX_HOURS_AHEAD = 44                  # markets for today and tomorrow
STOP_POSTING_MINUTES_BEFORE_CLOSE = 60

# --- Forecast model ---------------------------------------------------------------
# Ensemble = many slightly different runs of the same weather model. The share of
# runs landing in each temperature bracket is our starting probability.
ENSEMBLE_MODELS = {"ecmwf": "ecmwf_ifs025", "gfs": "gfs025"}   # 51 + 31 members
MODEL_WEIGHTS = {"ecmwf": 0.55, "gfs": 0.30, "nbm": 0.15}      # nbm = National Blend of Models
NBM_MODEL = "ncep_nbm_conus"
NBM_SD_F = 2.0                 # how uncertain we treat the single NBM forecast (°F)
KERNEL_SD_F = 1.3              # smoothing around each ensemble member (°F)
MIN_KERNEL_SD_F = 0.9
DEFAULT_BIAS_F = {"high": 0.8, "low": -0.6}   # hourly data misses peaks a bit; refined by calibration
OBS_TOLERANCE_F = 1.0          # observed reading -> official value uncertainty
FORECAST_REFRESH_MINUTES = 75  # ensembles update ~4x/day; don't hammer the free API
CALIBRATION_DAYS = 30          # history used to learn each station's bias
CALIBRATION_REFRESH_DAYS = 7
MODEL_WEIGHT_FOR_SIZING = 0.5  # shrink the model 50% toward the market price when sizing

# --- Strategies (each one is scored separately on the dashboard) -----------------
# 1) Longshot NO: sell the cheap 1-4c brackets the evening before (best-documented edge).
LONGSHOT = dict(enabled=True, local_hours=(14, 24), yes_bid_range=(0.01, 0.04),
                max_model_prob=0.03, max_no_price=0.98)
# 2) Favorite maker: rest a bid on a strong favorite the model agrees with.
FAVORITE = dict(enabled=True, price_range=(0.80, 0.95), min_model_prob=0.88, margin=0.03)
# 3) Model edge (experimental): model and market disagree a lot, and BOTH ensembles agree.
EDGE = dict(enabled=True, price_range=(0.08, 0.85), min_edge=0.12, min_each_model_edge=0.06,
            max_lead_hours=30)

# --- Sizing (aggressive: we're shooting for +50%) ---------------------------------
KELLY_FRACTION = 0.5           # half-Kelly on the (shrunk) model probability
MAX_BET_FRACTION = 0.10        # never more than 10% of the account on one market
MAX_EVENT_FRACTION = 0.25      # one city-day across all its brackets
MAX_TOTAL_EXPOSURE = 0.90      # orders + positions; keep 10% cash
MAX_OPEN_ORDERS = 40

# --- Paper fill simulation ----------------------------------------------------------
# A resting order only counts as filled when a real public trade prints at our price
# or better after we posted. Trades exactly AT our price only fill this share of
# their size (we might not be first in the queue).
FILL_AT_PRICE_SHARE = 0.5
MAKER_FEE_RATE = 0.0175
MAKER_FEE_SERIES_PREFIXES = []  # weather currently has no maker fee; add prefixes if that changes

# --- Plumbing ---------------------------------------------------------------------
STATE_DIR = "state/weather"
USER_AGENT = "kalshi-paper-bot/1.0 (github.com/xne0nlight)"   # NWS asks for an identifying UA
HTTP_TIMEOUT = 20
TIME_BUDGET_SECONDS = 300

# Station fallback if the NWS station lookup fails: CLI code -> (lat, lon, UTC standard offset)
FALLBACK_STATIONS = {
    "NYC": (40.7789, -73.9692, -5), "MDW": (41.7861, -87.7524, -6), "MIA": (25.7881, -80.3169, -5),
    "AUS": (30.1831, -97.6799, -6), "DEN": (39.8466, -104.6562, -7), "PHL": (39.8733, -75.2268, -5),
    "LAX": (33.9382, -118.3866, -8), "ATL": (33.6301, -84.4418, -5), "BOS": (42.3606, -71.0097, -5),
    "DFW": (32.8998, -97.0403, -6), "DAL": (32.8471, -96.8518, -6), "DCA": (38.8483, -77.0342, -5),
    "EWR": (40.6895, -74.1745, -5), "HOU": (29.6375, -95.2825, -6), "IAH": (29.9844, -95.3414, -6),
    "LAS": (36.0719, -115.1634, -8), "MSP": (44.8848, -93.2223, -6), "MSY": (29.9934, -90.2580, -6),
    "OKC": (35.3889, -97.6006, -6), "PHX": (33.4277, -112.0038, -7), "SAN": (32.7336, -117.1831, -8),
    "SAT": (29.5337, -98.4698, -6), "SDF": (38.1744, -85.7360, -5), "SEA": (47.4447, -122.3144, -8),
    "SFO": (37.6197, -122.3647, -8), "TTN": (40.2767, -74.8135, -5), "ORD": (41.9786, -87.9048, -6),
}

# Standard (non-daylight) UTC offsets for common US time zones, so we don't depend on
# the tz database (Windows Python doesn't ship one).
STANDARD_OFFSETS = {
    "America/New_York": -5, "America/Detroit": -5, "America/Kentucky/Louisville": -5,
    "America/Indiana/Indianapolis": -5, "America/Chicago": -6, "America/Denver": -7,
    "America/Boise": -7, "America/Phoenix": -7, "America/Los_Angeles": -8,
    "America/Anchorage": -9, "Pacific/Honolulu": -10,
}
