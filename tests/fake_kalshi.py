"""An offline stand-in for the Kalshi API that returns markets in the same
shape the real /markets endpoint documents (string *_dollars / *_fp fields)."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy  # noqa: E402

# --- Safety net -----------------------------------------------------------------
# Every test module imports this file first. Point ALL bots' storage at a throwaway
# folder before any test runs, so no test can ever touch the real paper accounts
# or dashboard, even if it forgets to pass its own temp folder.
import atexit  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import config  # noqa: E402
import sports_config  # noqa: E402
import weather_config  # noqa: E402

_SANDBOX = tempfile.mkdtemp(prefix="kalshi-tests-")
config.STATE_DIR = os.path.join(_SANDBOX, "state")
config.REPORT_PATH = os.path.join(_SANDBOX, "docs", "index.html")
weather_config.STATE_DIR = os.path.join(_SANDBOX, "state", "weather")
sports_config.STATE_DIR = os.path.join(_SANDBOX, "state", "sports")
atexit.register(shutil.rmtree, _SANDBOX, True)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_market(ticker, close_ts, yes_bid, yes_ask, event=None, title=None, sub="",
                vol24=1500, bid_size=400, ask_size=400, prev=None, market_type="binary"):
    prev_bid, prev_ask = prev if prev else (0, 0)
    return {
        "ticker": ticker,
        "event_ticker": event or ticker.rsplit("-", 1)[0],
        "market_type": market_type,
        "title": title or f"Market {ticker}",
        "yes_sub_title": sub,
        "no_sub_title": "",
        "status": "active",
        "close_time": iso(close_ts),
        "yes_bid_dollars": f"{yes_bid:.4f}",
        "yes_ask_dollars": f"{yes_ask:.4f}",
        "no_bid_dollars": f"{1 - yes_ask:.4f}",
        "no_ask_dollars": f"{1 - yes_bid:.4f}",
        "yes_bid_size_fp": f"{bid_size:.2f}",
        "yes_ask_size_fp": f"{ask_size:.2f}",
        "last_price_dollars": f"{yes_ask:.4f}",
        "previous_yes_bid_dollars": f"{prev_bid:.4f}",
        "previous_yes_ask_dollars": f"{prev_ask:.4f}",
        "previous_price_dollars": "0.0000",
        "volume_fp": f"{vol24 * 3:.2f}",
        "volume_24h_fp": f"{vol24:.2f}",
        "open_interest_fp": "1000.00",
        "result": "",
        "can_close_early": False,
        "rules_primary": "",
        "rules_secondary": "",
    }


class FakeKalshi:
    def __init__(self):
        self.markets = {}
        self.calls = 0
        self.trade_prints = {}   # ticker -> list of trade dicts

    def add_trade(self, ticker, ts, taker, yes_price, count):
        self.trade_prints.setdefault(ticker, []).append({
            "trade_id": f"t{len(self.trade_prints.get(ticker, []))}", "ticker": ticker,
            "count_fp": f"{count:.2f}", "yes_price_dollars": f"{yes_price:.4f}",
            "no_price_dollars": f"{1 - yes_price:.4f}", "taker_outcome_side": taker,
            "taker_book_side": "bid", "created_time": iso(ts) if isinstance(ts, (int, float)) else ts,
            "is_block_trade": False})

    def trades(self, ticker, min_ts=None, max_pages=10):
        self.calls += 1
        out = self.trade_prints.get(ticker, [])
        if min_ts:
            out = [t for t in out if strategy.parse_ts(t["created_time"]) > min_ts]
        return list(reversed(out))

    def add(self, *args, **kwargs):
        m = make_market(*args, **kwargs)
        self.markets[m["ticker"]] = m
        return m

    def set_quote(self, ticker, yes_bid, yes_ask):
        m = self.markets[ticker]
        m["yes_bid_dollars"] = f"{yes_bid:.4f}"
        m["yes_ask_dollars"] = f"{yes_ask:.4f}"
        m["no_bid_dollars"] = f"{1 - yes_ask:.4f}"
        m["no_ask_dollars"] = f"{1 - yes_bid:.4f}"

    def settle(self, ticker, result):
        m = self.markets[ticker]
        m["status"] = "finalized"
        m["result"] = result
        m["settlement_value_dollars"] = "1.0000" if result == "yes" else "0.0000"

    def markets_closing_between(self, start_ts, end_ts):
        self.calls += 1
        return [m for m in self.markets.values()
                if m["status"] == "active" and start_ts <= strategy.parse_ts(m["close_time"]) <= end_ts]

    def markets_for_series(self, series_ticker):
        self.calls += 1
        return [m for m in self.markets.values()
                if m["ticker"].startswith(series_ticker + "-") and m["status"] == "active"]

    def markets_by_ticker(self, tickers):
        self.calls += 1
        return {t: self.markets[t] for t in tickers if t in self.markets}

    def historical_market(self, ticker):
        return None
