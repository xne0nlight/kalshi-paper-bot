"""
Tiny read-only client for Kalshi's public market-data endpoints.

No API key needed: everything here is public data. Placing real orders
(a later phase) needs a signed API key and will live in its own module.
Standard library only, so there is nothing to pip install.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import config


class KalshiAPIError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


class KalshiPublicAPI:
    def __init__(self, base=config.API_BASE, pause=config.REQUEST_PAUSE_SEC, timeout=20):
        self.base = base.rstrip("/")
        self.pause = pause
        self.timeout = timeout
        self.calls = 0

    # -- low level -----------------------------------------------------------
    def _get(self, path, params=None):
        url = self.base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean)
        last_err = None
        for attempt in range(4):
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "kalshi-paper-bot/1.0"}
            )
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                time.sleep(self.pause)
                return data
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 429 or e.code >= 500:      # rate limited / server hiccup: back off
                    last_err = KalshiAPIError(e.code, body)
                    time.sleep(2 ** attempt)
                    continue
                raise KalshiAPIError(e.code, body)
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                time.sleep(2 ** attempt)
        raise last_err

    def _paginate(self, path, params, key="markets", max_pages=40):
        out, cursor = [], None
        for _ in range(max_pages):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            data = self._get(path, page_params)
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    # -- what the bot uses ---------------------------------------------------
    def markets_closing_between(self, start_ts, end_ts):
        """Open markets whose trading closes inside [start_ts, end_ts].

        Kalshi doesn't allow status=open together with close-time filters, so we
        ask by close time and keep the active ones ourselves.
        """
        params = {
            "min_close_ts": int(start_ts),
            "max_close_ts": int(end_ts),
            "limit": 1000,
            "mve_filter": "exclude",   # drop combo/parlay markets server-side
        }
        try:
            markets = self._paginate("/markets", params)
        except KalshiAPIError as e:
            if e.status != 400:
                raise
            params.pop("mve_filter")    # fall back if this filter combo is ever rejected
            markets = self._paginate("/markets", params)
        return [m for m in markets if m.get("status") in ("active", "open")]

    def markets_by_ticker(self, tickers):
        """Current snapshot (any status) for specific tickers -> {ticker: market}."""
        found = {}
        tickers = list(dict.fromkeys(tickers))
        for i in range(0, len(tickers), 50):
            batch = tickers[i:i + 50]
            for m in self._paginate("/markets", {"tickers": ",".join(batch), "limit": 1000}):
                found[m["ticker"]] = m
        return found

    def markets_for_series(self, series_ticker):
        """All open markets in one series (e.g. every upcoming NFL game)."""
        return self._paginate("/markets", {"series_ticker": series_ticker, "status": "open", "limit": 1000})

    def trades(self, ticker, min_ts=None, max_pages=10):
        """Public trade prints for one market (newest first), optionally after min_ts."""
        params = {"ticker": ticker, "limit": 1000}
        if min_ts:
            params["min_ts"] = int(min_ts)
        return self._paginate("/markets/trades", params, key="trades", max_pages=max_pages)

    def historical_market(self, ticker):
        """Markets settled a while ago move to the historical archive."""
        try:
            data = self._get("/historical/markets/" + urllib.parse.quote(ticker))
            return data.get("market")
        except KalshiAPIError as e:
            if e.status == 404:
                return None
            raise
