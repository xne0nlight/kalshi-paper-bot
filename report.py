"""Builds docs/index.html — a single static dashboard of the paper account."""
import html
import math
import os
from datetime import datetime, timezone

import config
import portfolio
import strategy

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(config.DISPLAY_TIMEZONE)
except Exception:  # Windows without the tzdata package -> fall back to UTC
    TZ = timezone.utc

esc = html.escape


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def fmt_dt(ts):
    if not ts:
        return "—"
    d = datetime.fromtimestamp(ts, TZ)
    hour = d.hour % 12 or 12
    return f"{d.strftime('%a %b')} {d.day}, {hour}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def fmt_in(seconds):
    if seconds <= 0:
        return "closed"
    h, m = int(seconds // 3600), int(seconds % 3600 // 60)
    return f"{h // 24}d {h % 24}h" if h >= 24 else (f"{h}h {m}m" if h else f"{m}m")


def money(x):
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def signed(x):
    return f"+${x:,.2f}" if x >= 0 else f"-${abs(x):,.2f}"


def cents(p):
    return f"{p * 100:.1f}¢" if abs(p * 100 - round(p * 100)) > 1e-6 else f"{p * 100:.0f}¢"


def tone(x):
    return "up" if x > 0.004 else ("down" if x < -0.004 else "flat")


# ---------------------------------------------------------------------------
# Evidence: are favorites beating their prices?
# ---------------------------------------------------------------------------
BUCKETS = [("80–85¢", 0.80, 0.85), ("85–90¢", 0.85, 0.90), ("90–95¢", 0.90, 0.9501),
           ("All", 0.0, 1.0)]


def calibration(watch):
    graded = [e for e in watch.get("entries", {}).values() if e.get("won") in (True, False)]
    rows = []
    for name, lo, hi in BUCKETS:
        es = [e for e in graded if lo <= e["price"] < hi]
        n = len(es)
        if not n:
            rows.append({"name": name, "n": 0})
            continue
        wins = sum(1 for e in es if e["won"])
        wr = wins / n
        breakeven = sum(e["price"] + strategy.fee_per_contract(e["price"]) for e in es) / n
        se = math.sqrt(max(wr * (1 - wr), 0.0025) / n)
        edge = wr - breakeven
        if n < 30:
            verdict, cls = "Too early to tell", "flat"
        elif edge > 2 * se:
            verdict, cls = "Beating the price", "up"
        elif edge < -2 * se:
            verdict, cls = "Losing after fees", "down"
        else:
            verdict, cls = "Inconclusive so far", "flat"
        rows.append({"name": name, "n": n, "wins": wins, "wr": wr, "be": breakeven,
                     "edge": edge, "verdict": verdict, "cls": cls})
    pending = sum(1 for e in watch.get("entries", {}).values() if e.get("won") is None)
    return rows, len(graded), pending


# ---------------------------------------------------------------------------
# Equity chart (inline SVG, no libraries)
# ---------------------------------------------------------------------------
def equity_svg(history, start):
    pts = [(t, mark) for t, _book, mark in history]
    if len(pts) < 2:
        return '<p class="muted chart-empty">The chart appears after the second run.</p>'
    W, H, PX, PY = 900, 240, 56, 22
    t0, t1 = pts[0][0], pts[-1][0]
    span = max(t1 - t0, 1)
    lo = min(min(v for _, v in pts), start)
    hi = max(max(v for _, v in pts), start)
    pad = max((hi - lo) * 0.15, 0.5)
    lo, hi = lo - pad, hi + pad

    def x(t):
        return PX + (t - t0) / span * (W - PX - 12)

    def y(v):
        return PY + (hi - v) / (hi - lo) * (H - 2 * PY)

    line = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in pts)
    area = f"{x(t0):.1f},{y(lo):.1f} {line} {x(t1):.1f},{y(lo):.1f}"
    up = pts[-1][1] >= start
    grid = ""
    for frac in (0, 0.5, 1):
        v = lo + (hi - lo) * frac
        grid += (f'<line x1="{PX}" x2="{W - 12}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                 f'<text x="{PX - 8}" y="{y(v) + 4:.1f}" class="axis" text-anchor="end">'
                 f'${v:,.2f}</text>')
    return f"""
<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Account value over time">
  {grid}
  <line x1="{PX}" x2="{W - 12}" y1="{y(start):.1f}" y2="{y(start):.1f}" class="baseline"/>
  <text x="{PX + 8}" y="{y(start) - 6:.1f}" class="axis">start {money(start)}</text>
  <polygon points="{area}" class="area {'up' if up else 'down'}"/>
  <polyline points="{line}" class="line {'up' if up else 'down'}"/>
  <circle cx="{x(t1):.1f}" cy="{y(pts[-1][1]):.1f}" r="4.5" class="dot {'up' if up else 'down'}"/>
</svg>
<div class="chart-foot muted"><span>{fmt_dt(t0)}</span><span>{fmt_dt(t1)}</span></div>"""


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
CSS = """
:root{--bg:#f6f5f1;--panel:#fff;--ink:#17181c;--muted:#6b6e76;--line:#e4e2dc;
--up:#13804b;--up-soft:rgba(19,128,75,.12);--down:#c2361f;--down-soft:rgba(194,54,31,.12);
--accent:#2f4bd8;--warn-bg:#fff4dc;--warn-ink:#7a4b00;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#111317;--panel:#1a1d23;
--ink:#eceef2;--muted:#9097a3;--line:#2a2e36;--up:#3fcf8e;--up-soft:rgba(63,207,142,.14);
--down:#ff7a63;--down-soft:rgba(255,122,99,.14);--accent:#8ea2ff;--warn-bg:#3a2c0e;
--warn-ink:#ffd48a;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:1120px;margin:0 auto;padding:28px 16px 64px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:12px;margin-bottom:22px}
h1{font-size:26px;letter-spacing:-.02em;margin:0}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 12px}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:3px 9px;border-radius:99px;
background:var(--warn-bg);color:var(--warn-ink);vertical-align:middle;margin-left:8px}
.muted{color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.kpi,.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px}
.kpi{padding:14px 16px}
.kpi .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.kpi .v{font-size:26px;font-weight:650;letter-spacing:-.02em;margin-top:2px}
.kpi .s{font-size:13px;color:var(--muted)}
.panel{padding:18px 18px 14px;margin-bottom:16px}
.alert{border-radius:12px;padding:12px 16px;margin-bottom:16px;background:var(--warn-bg);color:var(--warn-ink);font-weight:550}
.alert.err{background:var(--down-soft);color:var(--down)}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--muted)}
.table-wrap{overflow-x:auto;margin:0 -4px}
table{width:100%;border-collapse:collapse;font-size:14px;min-width:560px}
th{text-align:left;font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;
letter-spacing:.05em;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;white-space:nowrap}
.mkt{max-width:420px}
.side{font-size:11px;font-weight:700;padding:2px 7px;border-radius:6px;background:var(--line)}
.pill{font-size:12px;font-weight:650;padding:2px 8px;border-radius:99px}
.pill.up{background:var(--up-soft)}.pill.down{background:var(--down-soft)}.pill.flat{background:var(--line)}
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--line);stroke-width:1}
.chart .baseline{stroke:var(--muted);stroke-width:1.2;stroke-dasharray:5 5}
.chart .axis{fill:var(--muted);font-size:12px}
.chart .line{fill:none;stroke-width:2.5;stroke-linejoin:round}
.chart .line.up,.chart .dot.up{stroke:var(--up);fill:var(--up)}.chart .line.up{fill:none}
.chart .line.down,.chart .dot.down{stroke:var(--down);fill:var(--down)}.chart .line.down{fill:none}
.chart .area.up{fill:var(--up-soft)}.chart .area.down{fill:var(--down-soft)}
.chart-foot{display:flex;justify-content:space-between;font-size:12px;padding:4px 12px 0 56px}
.chart-empty{padding:40px 0;text-align:center}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:760px){.two{grid-template-columns:1fr}}
.bars{list-style:none;margin:0;padding:0}
.bars li{display:grid;grid-template-columns:1fr auto;gap:8px;font-size:14px;padding:4px 0}
.bars .bar{grid-column:1/-1;height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.bars .bar span{display:block;height:100%;background:var(--accent)}
.note{font-size:13px;color:var(--muted);margin:10px 0 0}
footer{font-size:12px;color:var(--muted);margin-top:28px;line-height:1.7}
"""


def _table(headers, rows, empty):
    if not rows:
        return f'<p class="muted">{empty}</p>'
    head = "".join(f'<th class="{c}">{h}</th>' for h, c in headers)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def build_html(state, watch, now_ts=None):
    import time
    now_ts = now_ts or time.time()
    start = state["starting_bankroll"]
    mark = portfolio.mark_equity(state)
    book = portfolio.book_equity(state)
    total_pnl = mark - start
    closed = state["closed_positions"]
    wins = sum(1 for p in closed if p["pnl"] > 0)
    losses = len(closed) - wins
    realized = sum(p["pnl"] for p in closed)
    in_play = portfolio.open_cost(state)
    run = state.get("last_run") or {}
    days = max((now_ts - state["created_ts"]) / 86400, 0)

    alerts = ""
    if run.get("halt"):
        alerts += f'<div class="alert">⏸ {esc(run["halt"])}</div>'
    if run.get("error"):
        alerts += f'<div class="alert err">Last run hit an error: {esc(run["error"])}</div>'

    kpis = f"""
<div class="kpis">
  <div class="kpi"><div class="k">Account value</div><div class="v">{money(mark)}</div>
    <div class="s {tone(total_pnl)}">{signed(total_pnl)} ({total_pnl / start:+.1%})</div></div>
  <div class="kpi"><div class="k">Realized profit</div><div class="v {tone(realized)}">{signed(realized)}</div>
    <div class="s">after {money(state['fees_paid'])} in fees</div></div>
  <div class="kpi"><div class="k">Record</div><div class="v">{wins}–{losses}</div>
    <div class="s">{(wins / len(closed)) if closed else 0:.0%} win rate · {len(closed)} settled</div></div>
  <div class="kpi"><div class="k">In play</div><div class="v">{money(in_play)}</div>
    <div class="s">{len(state['open_positions'])} open · {money(state['cash'])} cash</div></div>
</div>"""

    # open positions
    open_rows = []
    for p in sorted(state["open_positions"], key=lambda p: p["close_ts"]):
        mk = p.get("mark_price", p["price"])
        unreal = p["contracts"] * mk - p["cost"]
        open_rows.append(
            f'<tr><td class="mkt">{esc(p["label"])}</td><td><span class="side">{p["side"].upper()}</span></td>'
            f'<td class="num">{p["contracts"]}</td><td class="num">{cents(p["price"])}</td>'
            f'<td class="num">{money(p["cost"])}</td><td class="num">{cents(mk)}</td>'
            f'<td class="num {tone(unreal)}">{signed(unreal)}</td>'
            f'<td class="num">{fmt_in(p["close_ts"] - now_ts)}</td>'
            f'<td class="num">{money(p["contracts"] - p["cost"])}</td></tr>')
    open_tbl = _table([("Market", ""), ("Side", ""), ("Qty", "num"), ("Paid", "num"),
                       ("Cost", "num"), ("Bid now", "num"), ("If sold now", "num"),
                       ("Closes", "num"), ("Profit if win", "num")],
                      open_rows, "No open bets right now.")

    # settled
    closed_rows = []
    for p in sorted(closed, key=lambda p: p["closed_ts"], reverse=True)[:25]:
        closed_rows.append(
            f'<tr><td class="mkt">{esc(p["label"])}</td><td><span class="side">{p["side"].upper()}</span></td>'
            f'<td class="num">{p["contracts"]}</td><td class="num">{cents(p["price"])}</td>'
            f'<td class="num"><span class="pill {"up" if p["pnl"] > 0 else "down"}">'
            f'{"WON" if p["pnl"] > 0 else "LOST"}</span></td>'
            f'<td class="num {tone(p["pnl"])}">{signed(p["pnl"])}</td>'
            f'<td class="num">{fmt_dt(p["closed_ts"])}</td></tr>')
    closed_tbl = _table([("Market", ""), ("Side", ""), ("Qty", "num"), ("Paid", "num"),
                         ("Result", "num"), ("Profit", "num"), ("Settled", "num")],
                        closed_rows, "Nothing has settled yet — most bets resolve within a day.")

    # evidence
    cal, n_graded, n_pending = calibration(watch)
    cal_rows = []
    for r in cal:
        if not r["n"]:
            cal_rows.append(f'<tr><td>{r["name"]}</td><td class="num">0</td>'
                            f'<td class="num">—</td><td class="num">—</td><td class="num">—</td>'
                            f'<td class="num"><span class="pill flat">No data yet</span></td></tr>')
            continue
        cal_rows.append(
            f'<tr><td>{r["name"]}</td><td class="num">{r["n"]}</td>'
            f'<td class="num">{r["wr"]:.1%}</td><td class="num">{r["be"]:.1%}</td>'
            f'<td class="num {r["cls"]}">{r["edge"] * 100:+.1f} pts</td>'
            f'<td class="num"><span class="pill {r["cls"]}">{r["verdict"]}</span></td></tr>')
    cal_tbl = _table([("Price range", ""), ("Markets graded", "num"), ("Favorite won", "num"),
                      ("Needed to break even", "num"), ("Edge", "num"), ("Verdict", "num")],
                     cal_rows, "")

    # last scan
    rej = run.get("rejections") or {}
    total_rej = sum(rej.values()) or 1
    rej_items = "".join(
        f'<li><span>{esc(k)}</span><span class="muted">{v:,}</span>'
        f'<div class="bar"><span style="width:{v / total_rej * 100:.1f}%"></span></div></li>'
        for k, v in list(rej.items())[:7]) or '<li class="muted">No scan yet.</li>'
    top = "".join(
        f'<li><span>{esc(c["label"])} <span class="side">{c["side"].upper()}</span></span>'
        f'<span class="muted">{cents(c["price"])} · {c["hours_to_close"]:.1f}h</span></li>'
        for c in run.get("top_candidates", [])) or '<li class="muted">No qualifying markets last scan.</li>'

    cfg = (f"Bets {config.MIN_PRICE * 100:.0f}–{config.MAX_PRICE * 100:.0f}¢ favorites closing in "
           f"{config.MIN_HOURS_TO_CLOSE * 60:.0f} min–{config.MAX_HOURS_TO_CLOSE}h · max "
           f"{config.RISK_PER_TRADE:.0%} per bet, {config.MAX_TOTAL_EXPOSURE:.0%} total in play · "
           f"spread ≤ {config.MAX_SPREAD * 100:.0f}¢ · ≥ {config.MIN_VOLUME_24H} contracts/day · "
           f"daily stop {config.DAILY_STOP_LOSS:.0%}, drawdown brake {config.MAX_DRAWDOWN_HALT:.0%}")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Kalshi Paper Bot</title><style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <div><h1>Kalshi Paper Bot <span class="badge">PAPER MONEY</span></h1>
  <div class="muted">Imaginary {money(start)} · started {fmt_dt(state['created_ts'])} · day {days:.1f}</div></div>
  <div class="muted">Last run {fmt_dt(run.get('ts'))}<br>Book value {money(book)}</div>
</header>
{alerts}
{kpis}
<section class="panel"><h2>Account value</h2>{equity_svg(state['equity_history'], start)}</section>
<section class="panel"><h2>Is the edge real?</h2>
  <p class="muted" style="margin-top:-4px">Every market that passed the filters is tracked here,
  bet or not, and graded when it settles. Favorites must win more often than
  “needed to break even” (price + fee) for this strategy to make money.</p>
  {cal_tbl}
  <p class="note">{n_graded:,} graded · {n_pending:,} still waiting to settle. Verdicts need 30+ markets and a
  gap of 2 standard errors before they call it either way.</p>
</section>
<section class="panel"><h2>Open bets</h2>{open_tbl}</section>
<section class="panel"><h2>Recent settled bets</h2>{closed_tbl}
  {f'<p class="note">Showing the latest 25 of {len(closed):,}. Full history: state/trade_log.csv</p>' if len(closed) > 25 else ''}</section>
<div class="two">
  <section class="panel"><h2>Last scan</h2>
    <p class="muted" style="margin-top:-4px">{run.get('markets_scanned', 0):,} markets closing soon ·
    {run.get('candidates', 0):,} qualified · {len(run.get('opened', []))} bet{'' if len(run.get('opened', [])) == 1 else 's'} placed</p>
    <ul class="bars">{rej_items}</ul></section>
  <section class="panel"><h2>Top-ranked right now</h2><ul class="bars">{top}</ul></section>
</div>
<footer>{esc(cfg)}<br>Fills are simulated at the best ask with Kalshi's taker fee and held to
settlement. Real fills can be worse. Not financial advice — it's a robot with imaginary money.</footer>
</div></body></html>"""


def write_report(state, watch, path, now_ts=None):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_html(state, watch, now_ts))
