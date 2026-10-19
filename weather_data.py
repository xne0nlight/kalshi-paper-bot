"""
Free weather data sources for the weather bot. No API keys needed.

  Open-Meteo Ensemble API  - ECMWF (51 runs) + GFS (31 runs) hourly forecasts
  Open-Meteo Forecast API  - NBM (National Blend of Models, what NWS forecasters start from)
  Open-Meteo Historical Forecast API - past forecasts, to measure each station's bias
  NWS api.weather.gov      - station coordinates/time zone + live observations today
  Iowa Environmental Mesonet - archive of official NWS daily climate reports (the actual highs/lows)

Every fetch is cached and fails soft: a broken source makes the bot skip that
input (and shows up on the dashboard), it never crashes the run.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import weather_config as wc
import weather_model as wm

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
NWS_URL = "https://api.weather.gov"
IEM_CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py"


def http_json(url, params=None, retries=2):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": wc.USER_AGENT,
                                                   "Accept": "application/geo+json, application/json"})
        try:
            with urllib.request.urlopen(req, timeout=wc.HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last


# ---------------------------------------------------------------------------
# Parsers (kept separate so tests can feed them sample JSON)
# ---------------------------------------------------------------------------
def parse_hourly_members(data):
    """Open-Meteo hourly block -> (times, [member series...]). Every key starting with
    'temperature_2m' is one ensemble member (control run + perturbed members)."""
    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    members = [v for k, v in sorted(hourly.items())
               if k.startswith("temperature_2m") and isinstance(v, list) and len(v) == len(times)]
    return times, members


def parse_nws_station(data):
    props = (data or {}).get("properties") or {}
    coords = ((data or {}).get("geometry") or {}).get("coordinates") or []
    tz = props.get("timeZone")
    if len(coords) < 2 or tz not in wc.STANDARD_OFFSETS:
        return None
    return {"lat": coords[1], "lon": coords[0], "tz": tz, "std_offset": wc.STANDARD_OFFSETS[tz],
            "dst": tz not in ("America/Phoenix", "Pacific/Honolulu"), "name": props.get("name")}


def parse_nws_observations(data, start_ts):
    """-> (max_f, min_f, count) of temperatures observed since start_ts."""
    temps = []
    for f in (data or {}).get("features") or []:
        p = f.get("properties") or {}
        t = p.get("temperature") or {}
        v = t.get("value")
        ts = p.get("timestamp")
        if v is None or not ts:
            continue
        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        if when < start_ts:
            continue
        unit = t.get("unitCode", "")
        temps.append(v * 9 / 5 + 32 if unit.endswith("degC") else v)
    if not temps:
        return None, None, 0
    return max(temps), min(temps), len(temps)


def parse_iem_cli(data):
    """IEM climate report archive -> {date: {'high': int, 'low': int}} (skips missing 'M')."""
    out = {}
    for r in (data or {}).get("results") or []:
        d = r.get("valid")
        rec = {}
        for k in ("high", "low"):
            v = r.get(k)
            if isinstance(v, (int, float)):
                rec[k] = v
        if d and rec:
            out[d] = rec
    return out


# ---------------------------------------------------------------------------
# Fetching with caching
# ---------------------------------------------------------------------------
class WeatherData:
    def __init__(self, cache, now_ts):
        self.cache = cache
        self.now = now_ts
        self.health = {}
        self._lock = threading.Lock()
        for k in ("stations", "forecast", "nbm", "climate", "calibration"):
            cache.setdefault(k, {})

    def _ok(self, source, ok, err=None):
        with self._lock:
            h = self.health.setdefault(source, {"ok": 0, "fail": 0, "last_error": None})
            if ok:
                h["ok"] += 1
            else:
                h["fail"] += 1
                h["last_error"] = str(err)[:160]

    # -- stations -------------------------------------------------------------
    def station(self, code):
        st = self.cache["stations"].get(code)
        if st and st.get("src") == "nws":
            return st
        try:
            st = parse_nws_station(http_json(f"{NWS_URL}/stations/K{code}"))
            self._ok("nws_station", st is not None, "unknown time zone")
            if st:
                st.update(src="nws", code=code)
                self.cache["stations"][code] = st
                return st
        except Exception as e:  # noqa: BLE001
            self._ok("nws_station", False, e)
        fb = wc.FALLBACK_STATIONS.get(code)
        if fb:
            st = {"lat": fb[0], "lon": fb[1], "std_offset": fb[2], "dst": code != "PHX",
                  "src": "fallback", "code": code}
            self.cache["stations"][code] = st
            return st
        return None

    # -- ensemble forecast --------------------------------------------------------
    def forecast_extremes(self, code, st):
        """{date: {'high': {'ecmwf': [...], 'gfs': [...]}, 'low': {...}}} for the next few
        climate days at this station. Refreshed at most every FORECAST_REFRESH_MINUTES."""
        entry = self.cache["forecast"].get(code)
        if entry and self.now - entry.get("ts", 0) < wc.FORECAST_REFRESH_MINUTES * 60:
            return entry["days"]
        days = {}
        for name, model in wc.ENSEMBLE_MODELS.items():
            try:
                data = http_json(ENSEMBLE_URL, {
                    "latitude": st["lat"], "longitude": st["lon"], "hourly": "temperature_2m",
                    "models": model, "temperature_unit": "fahrenheit", "timeformat": "unixtime",
                    "past_days": 1, "forecast_days": 3})
                times, members = parse_hourly_members(data)
                self._ok("open_meteo_ensemble", bool(members), "no members in response")
                self._extremes_into(days, name, times, members, st)
            except Exception as e:  # noqa: BLE001
                self._ok("open_meteo_ensemble", False, e)
        if days:
            self.cache["forecast"][code] = {"ts": round(self.now), "days": days}
            return days
        return entry["days"] if entry else {}   # stale beats nothing

    def _extremes_into(self, days, name, times, members, st):
        for offset in (-1, 0, 1, 2):
            d = (datetime.fromtimestamp(self.now, timezone.utc)
                 + timedelta(hours=st["std_offset"], days=offset)).strftime("%Y-%m-%d")
            start, end = wm.climate_day_window(d, st["std_offset"])
            for kind in ("high", "low"):
                vals = [wm.window_extreme(times, m, start, end, kind) for m in members]
                vals = [round(v, 1) for v in vals if v is not None]
                if vals:
                    days.setdefault(d, {}).setdefault(kind, {})[name] = vals

    # -- National Blend of Models (single best-guess forecast) ----------------------
    def nbm_extremes(self, code, st):
        entry = self.cache["nbm"].get(code)
        if entry and self.now - entry.get("ts", 0) < wc.FORECAST_REFRESH_MINUTES * 60:
            return entry["days"]
        days = {}
        try:
            data = http_json(FORECAST_URL, {
                "latitude": st["lat"], "longitude": st["lon"], "hourly": "temperature_2m",
                "models": wc.NBM_MODEL, "temperature_unit": "fahrenheit", "timeformat": "unixtime",
                "past_days": 1, "forecast_days": 3})
            times, series = parse_hourly_members(data)
            self._ok("open_meteo_nbm", bool(series), "no data")
            if series:
                tmp = {}
                self._extremes_into(tmp, "nbm", times, series[:1], st)
                days = {d: {k: v["nbm"][0] for k, v in kinds.items()} for d, kinds in tmp.items()}
        except Exception as e:  # noqa: BLE001
            self._ok("open_meteo_nbm", False, e)
        if days:
            self.cache["nbm"][code] = {"ts": round(self.now), "days": days}
            return days
        return entry["days"] if entry else {}

    # -- live observations (today only) --------------------------------------------
    def observed_so_far(self, code, start_ts):
        if self.now <= start_ts:
            return None, None
        try:
            iso = datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data = http_json(f"{NWS_URL}/stations/K{code}/observations", {"start": iso})
            hi, lo, n = parse_nws_observations(data, start_ts)
            self._ok("nws_observations", True)
            return (hi, lo) if n else (None, None)
        except Exception as e:  # noqa: BLE001
            self._ok("nws_observations", False, e)
            return None, None

    # -- actual official highs/lows (for calibration & scoring) -----------------------
    def climate_actuals(self, code, year):
        key = f"{code}|{year}"
        entry = self.cache["climate"].get(key)
        if entry and self.now - entry.get("ts", 0) < 6 * 3600:
            return entry["days"]
        try:
            days = parse_iem_cli(http_json(IEM_CLI_URL, {"station": f"K{code}", "year": year}))
            self._ok("iem_climate", True)
            # keep only the last ~60 days so the cache stays small
            cutoff = (datetime.fromtimestamp(self.now, timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
            days = {d: v for d, v in days.items() if d >= cutoff}
            self.cache["climate"][key] = {"ts": round(self.now), "days": days}
            return days
        except Exception as e:  # noqa: BLE001
            self._ok("iem_climate", False, e)
            return entry["days"] if entry else {}

    # -- station bias from history (refreshed weekly) ------------------------------------
    def calibration(self, code, st):
        cal = self.cache["calibration"].get(code)
        if cal and self.now - cal.get("ts", 0) < wc.CALIBRATION_REFRESH_DAYS * 86400:
            return cal
        today = datetime.fromtimestamp(self.now, timezone.utc).date()
        start = today - timedelta(days=wc.CALIBRATION_DAYS)
        end = today - timedelta(days=2)
        try:
            data = http_json(HISTORICAL_URL, {
                "latitude": st["lat"], "longitude": st["lon"], "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit", "timeformat": "unixtime",
                "start_date": start.isoformat(), "end_date": end.isoformat()})
            times, series = parse_hourly_members(data)
            if not series:
                raise ValueError("no historical series")
            actual = {}
            for y in {start.year, end.year}:
                actual.update(self.climate_actuals(code, y))
            out = {"ts": round(self.now)}
            for kind in ("high", "low"):
                pairs = []
                d = start
                while d <= end:
                    ds = d.isoformat()
                    s, e = wm.climate_day_window(ds, st["std_offset"])
                    fc = wm.window_extreme(times, series[0], s, e, kind)
                    act = (actual.get(ds) or {}).get(kind)
                    if fc is not None and act is not None:
                        pairs.append((fc, act))
                    d += timedelta(days=1)
                out[kind] = wm.calibrate(pairs)
            self._ok("open_meteo_history", True)
            self.cache["calibration"][code] = out
            return out
        except Exception as e:  # noqa: BLE001
            self._ok("open_meteo_history", False, e)
            if cal:
                return cal
            # remember the failure for an hour so we don't retry every station every run
            self.cache["calibration"][code] = {"ts": round(self.now - wc.CALIBRATION_REFRESH_DAYS * 86400 + 3600)}
            return self.cache["calibration"][code]
