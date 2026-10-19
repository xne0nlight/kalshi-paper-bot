"""The Sports tab of the dashboard."""
import html
import time

import report as r
import sports_bot as sb
import sports_config as sc

esc = html.escape
STATUS_PILL = {"won": "up", "lost": "down", "banked": "flat", "void": "flat", "active": "flat", "skipped": "flat"}
LEG_ICON = {"planned": "○", "ordered": "◔", "filled": "●", "won": "✓", "lost": "✗", "skipped": "–"}


def account_value(state):
    return sb.rolling_equity(state, mark=True) if state else None


def _pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def _two_line_chart(state):
    """Rolling vs combo account value on one chart."""
    a, b = state["rolling"]["history"], state["combo"]["history"]
    if len(a) < 2:
        return '<p class="muted chart-empty">The chart appears after the second run.</p>'
    W, H, PX, PY = 900, 220, 56, 20
    t0, t1 = a[0][0], a[-1][0]
    vals = [x[2] for x in a] + [x[1] for x in b] + [sc.STARTING_BANKROLL]
    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * 0.15, 0.5)
    lo, hi = lo - pad, hi + pad

    def x(t):
        return PX + (t - t0) / max(t1 - t0, 1) * (W - PX - 12)

    def y(v):
        return PY + (hi - v) / (hi - lo) * (H - 2 * PY)

    line_a = " ".join(f"{x(t):.1f},{y(m):.1f}" for t, _bk, m in a)
    line_b = " ".join(f"{x(t):.1f},{y(bk):.1f}" for t, bk, _m in b)
    grid = "".join(f'<line x1="{PX}" x2="{W - 12}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                   f'<text x="{PX - 8}" y="{y(v) + 4:.1f}" class="axis" text-anchor="end">${v:,.0f}</text>'
                   for v in (lo + (hi - lo) * f for f in (0, 0.5, 1)))
    return f"""<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Rolling vs combo account value">
  {grid}<line x1="{PX}" x2="{W - 12}" y1="{y(sc.STARTING_BANKROLL):.1f}" y2="{y(sc.STARTING_BANKROLL):.1f}" class="baseline"/>
  <polyline points="{line_b}" style="fill:none;stroke:var(--muted);stroke-width:2;stroke-dasharray:6 4"/>
  <polyline points="{line_a}" style="fill:none;stroke:var(--accent);stroke-width:2.5"/>
</svg>
<div class="chart-foot muted"><span><span style="color:var(--accent)">━</span> Rolling parlays
 &nbsp; <span>┅</span> Kalshi combos (simulated)</span><span>{r.fmt_dt(t1)}</span></div>"""


def _legs_html(p, now_ts):
    out = []
    for i, l in enumerate(p["legs"]):
        cur = p["status"] == "active" and i == p["cur"]
        price = l["cost"] / l["contracts"] if l["contracts"] else (l["order"] or {}).get("price") or l["price_plan"]
        extra = ""
        if l["status"] == "ordered" and l.get("order"):
            o = l["order"]
            extra = f' · bid {r.cents(o["price"])} ({o["filled"]:g}/{o["contracts"]:g} filled)'
        style = ' style="font-weight:600"' if cur else ""     # (kept outside the f-string for Python 3.10)
        out.append(f'<div{style}>{LEG_ICON.get(l["status"], "?")} '
                   f'{esc(l["league"])} <b>{esc(l["team"])}</b> vs {esc(l["opp"])} · {r.cents(price)} '
                   f'<span class="muted">(books {_pct(l["q"])}) · {r.fmt_dt(l["start_ts"])}{extra}</span></div>')
    return "".join(out)


def parlay_rows(parlays, now_ts):
    rows = []
    for p in parlays:
        c = p["combo"]
        roll = (f'<span class="pill {STATUS_PILL[p["status"]]}">{p["status"].upper()}</span>'
                + (f' <span class="{r.tone(p["pnl"])}">{r.signed(p["pnl"])}</span>' if p["pnl"] is not None else
                   f' <span class="muted">riding {r.money(sb.rolling_equity({"rolling": {"cash": 0}, "parlays": [p]}))}</span>'))
        combo = ("—" if c["status"] == "skipped" else
                 f'<span class="pill {STATUS_PILL.get(c["status"], "flat")}">{c["status"].upper()}</span>'
                 + (f' <span class="{r.tone(c["pnl"])}">{r.signed(c["pnl"])}</span>' if c["pnl"] is not None else
                    f' <span class="muted">{r.money(c["stake"])} at {r.cents(c["price"])}</span>'))
        rows.append(f'<tr><td class="mkt">{_legs_html(p, now_ts)}'
                    f'<div class="muted">{p["note"] and esc(p["note"]) + " · "}parlay #{p["id"]} · started {r.fmt_dt(p["created_ts"])}</div></td>'
                    f'<td class="num">{r.money(p["stake"])}</td><td class="num">{1 / p["p_par"]:.1f}x</td>'
                    f'<td class="num">{_pct(p["q_par"])}</td><td class="num">{roll}</td><td class="num">{combo}</td></tr>')
    return rows


def board_table(run, now_ts):
    rows = []
    for b in [x for x in (run.get("board") or []) if x.get("q") is not None][:60]:
        edge = f'{(b["q"] - b["mid"]) * 100:+.0f}' if b.get("mid") is not None else "—"
        cls = "up" if b["ok"] else "flat"
        rec = f' <span class="muted">({esc(b["record"])})</span>' if b.get("record") else ""
        rows.append(f'<tr><td>{esc(b["league"])}</td><td class="mkt"><b>{esc(b["team"])}</b>{rec} vs {esc(b["opp"])}</td>'
                    f'<td class="num">{r.fmt_dt(b["start_ts"])}</td>'
                    f'<td class="num">{r.cents(b["bid"]) if b["bid"] is not None else "—"} / '
                    f'{r.cents(b["ask"]) if b["ask"] is not None else "—"}</td>'
                    f'<td class="num">{_pct(b["q"])} <span class="muted">({b["books"]})</span></td>'
                    f'<td class="num {r.tone((b["q"] - b["mid"]) if b.get("mid") is not None else 0)}">{edge}</td>'
                    f'<td class="num"><span class="pill {cls}">{esc(b["why"])}</span></td></tr>')
    return r._table([("League", ""), ("Team", ""), ("Starts", "num"), ("Kalshi bid / ask", "num"),
                     ("Books' fair %", "num"), ("Gap (pts)", "num"), ("Status", "num")], rows,
                    "No games with sportsbook odds yet. (Is the ODDS_API_KEY secret set?)")


def scorecard_table(state):
    tot = (state.get("scorecard") or {}).get("totals") or {}
    rows = []
    for key in ["all"] + list(sc.LEAGUES):
        t = tot.get(key)
        name = "All games" if key == "all" else key
        if not t or not t["n"]:
            if key == "all":
                rows.append(f'<tr><td>{name}</td><td class="num">0</td><td class="num">—</td><td class="num">—</td>'
                            f'<td class="num"><span class="pill flat">No data yet</span></td></tr>')
            continue
        bb, kb = t["books"] / t["n"], t["kalshi"] / t["n"]
        if t["n"] < 150:
            v, cls = "Too early to tell", "flat"
        elif bb < kb * 0.98:
            v, cls = "Books more accurate (edge exists)", "up"
        elif bb > kb * 1.02:
            v, cls = "Kalshi more accurate", "down"
        else:
            v, cls = "About even", "flat"
        rows.append(f'<tr><td>{name}</td><td class="num">{t["n"]:,}</td><td class="num">{bb:.4f}</td>'
                    f'<td class="num">{kb:.4f}</td><td class="num"><span class="pill {cls}">{v}</span></td></tr>')
    return r._table([("Games", ""), ("Graded", "num"), ("Books error", "num"), ("Kalshi error", "num"),
                     ("Verdict", "num")], rows, "")


def sports_body(state, now_ts=None):
    now_ts = now_ts or time.time()
    if not state:
        return ('<header><div><h1>Sports parlay bot <span class="badge">PAPER MONEY</span></h1>'
                '<div class="muted">Hasn\'t run yet. It starts on the next scheduled run.</div></div></header>')
    run = state.get("last_run") or {}
    roll_mark, roll_book = sb.rolling_equity(state, True), sb.rolling_equity(state)
    combo = sb.combo_equity(state)
    start = sc.STARTING_BANKROLL
    done = [p for p in state["parlays"] if p["status"] not in ("active",)]
    rw = sum(1 for p in done if p["status"] == "won")
    rl = sum(1 for p in done if p["status"] == "lost")
    rb = sum(1 for p in done if p["status"] in ("banked",))
    cdone = [p for p in state["parlays"] if p["combo"]["status"] in ("won", "lost")]
    cw = sum(1 for p in cdone if p["combo"]["status"] == "won")
    active = [p for p in state["parlays"] if p["status"] == "active" or p["combo"]["status"] == "active"]
    recent = sorted([p for p in state["parlays"] if p not in active], key=lambda p: p["closed_ts"] or 0, reverse=True)[:20]
    budget = run.get("budget") or {}

    alerts = ""
    if not budget.get("has_key", True):
        alerts += ('<div class="alert">No ODDS_API_KEY found, so the bot can\'t see sportsbook odds yet. '
                   'Add the GitHub secret (see README) and it starts on the next run.</div>')
    if run.get("halt"):
        alerts += f'<div class="alert">⏸ {esc(run["halt"])}</div>'
    if run.get("error"):
        alerts += f'<div class="alert err">Last run hit an error: {esc(run["error"])}</div>'
    for k, v in (run.get("health") or {}).items():
        if v["fail"] and not v["ok"]:
            alerts += f'<div class="alert">{esc(k)} failed on the last run: {esc(v.get("last_error") or "")}</div>'

    kpis = f"""
<div class="kpis">
  <div class="kpi"><div class="k">Rolling parlays</div><div class="v">{r.money(roll_mark)}</div>
    <div class="s {r.tone(roll_mark - start)}">{r.signed(roll_mark - start)} · {rw}W {rl}L {rb} banked</div></div>
  <div class="kpi"><div class="k">Kalshi combos (simulated)</div><div class="v">{r.money(combo)}</div>
    <div class="s {r.tone(combo - start)}">{r.signed(combo - start)} · {cw}W {len(cdone) - cw}L</div></div>
  <div class="kpi"><div class="k">Rolling vs combo</div><div class="v {r.tone(roll_mark - combo)}">{r.signed(roll_mark - combo)}</div>
    <div class="s">what skipping the combo cut is worth so far</div></div>
  <div class="kpi"><div class="k">Live parlays</div><div class="v">{len([p for p in state['parlays'] if p['status'] == 'active'])}</div>
    <div class="s">{run.get('eligible', 0)} eligible legs · {run.get('games', 0)} games scanned</div></div>
</div>"""

    active_tbl = r._table([("Parlay legs (● current)", ""), ("Stake", "num"), ("Pays", "num"), ("Hit chance", "num"),
                           ("Rolling", "num"), ("Combo", "num")], parlay_rows(active, now_ts),
                          "No live parlays. New ones start when 2+ legs with an edge line up within a day.")
    recent_tbl = r._table([("Parlay legs", ""), ("Stake", "num"), ("Pays", "num"), ("Hit chance", "num"),
                           ("Rolling", "num"), ("Combo", "num")], parlay_rows(recent, now_ts), "No finished parlays yet.")
    cr = budget.get("remaining")
    credit_note = (f"Odds API credits left this month: {cr} · used today: {budget.get('today', 0)}"
                   if cr is not None else "Odds API credits: not checked yet")
    cfg = (f"Legs: team to win, books' fair chance {sc.LEG_PROB_RANGE[0]:.0%}–{sc.LEG_PROB_RANGE[1]:.0%} and at least "
           f"{sc.LEG_MIN_EDGE * 100:.0f} pts better than our Kalshi price. Parlays {sc.MIN_LEGS}–{sc.MAX_LEGS} legs paying "
           f"{1 / sc.PARLAY_PRICE_RANGE[1]:.1f}x–{1 / sc.PARLAY_PRICE_RANGE[0]:.0f}x, quarter-Kelly, max "
           f"{sc.MAX_STAKE_FRACTION:.0%} per parlay. Rolling legs are limit orders; fills only count when real trades "
           f"print at our price. Combos priced at {sc.COMBO_HOLD:.0%} over fair, Kalshi's historical combo cut.")

    return f"""<header>
  <div><h1>Sports parlay bot <span class="badge">PAPER MONEY</span></h1>
  <div class="muted">Two imaginary {r.money(start)} accounts, same picks · started {r.fmt_dt(state['created_ts'])} · {esc(credit_note)}</div></div>
  <div class="muted">Last run {r.fmt_dt(run.get('ts'))}<br>Rolling book value {r.money(roll_book)}</div>
</header>
{alerts}
{kpis}
<section class="panel"><h2>Account value</h2>{_two_line_chart(state)}</section>
<section class="panel"><h2>Live parlays</h2>
  <p class="muted" style="margin-top:-4px"><b>Rolling:</b> leg 1 is a limit order; if it wins, everything rides on leg 2,
  and so on. If a later leg can't be bought at a good price, the chain stops and keeps its winnings ("banked").
  <b>Combo:</b> the same legs as one Kalshi parlay, all locked in up front.</p>
  {active_tbl}</section>
<section class="panel"><h2>Recently finished</h2>{recent_tbl}</section>
<section class="panel"><h2>Are the sportsbooks beating Kalshi?</h2>
  <p class="muted" style="margin-top:-4px">Every team-to-win market we match is graded after the game: the books' fair
  probability vs Kalshi's price from before kickoff (lower error = more accurate). The whole strategy assumes the books
  win this. If Kalshi wins it, there's no edge to bet.</p>
  {scorecard_table(state)}</section>
<section class="panel"><h2>Game board</h2>
  <p class="muted" style="margin-top:-4px">Upcoming games with sportsbook odds. "Gap" = books' fair % minus Kalshi's mid
  price, in points. Records from ESPN.</p>
  {board_table(run, now_ts)}</section>
<footer>{esc(cfg)}<br>Paper trading only; nothing is sent to Kalshi. Not financial advice.</footer>"""
