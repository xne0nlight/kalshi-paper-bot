"""The Weather tab of the dashboard."""
import html
import time

import report as r
import weather_bot as wb
import weather_config as wc

esc = html.escape


def account_value(state):
    return wb.mark_equity(state) if state else None


def _pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def strategy_rows(state):
    rows = []
    for key in wb.STRATEGIES:
        closed = [p for p in state["closed"] if p.get("strategy") == key]
        wins = sum(1 for p in closed if p["pnl"] > 0)
        pnl = sum(p["pnl"] for p in closed)
        staked = sum(p["cost"] for p in closed)
        resting = sum(1 for o in state["orders"] if o.get("strategy") == key)
        held = sum(1 for p in state["positions"] if p.get("strategy") == key)
        per = f"{pnl / staked * 100:+.1f}%" if staked else "—"
        rows.append(
            f'<tr><td>{wb.STRATEGY_NAMES[key]}</td><td class="num">{len(closed)}</td>'
            f'<td class="num">{wins}–{len(closed) - wins}</td>'
            f'<td class="num {r.tone(pnl)}">{r.signed(pnl)}</td><td class="num">{r.money(staked)}</td>'
            f'<td class="num {r.tone(pnl)}">{per}</td><td class="num">{resting}</td><td class="num">{held}</td></tr>')
    return r._table([("Strategy", ""), ("Settled", "num"), ("Record", "num"), ("Profit", "num"),
                     ("Staked", "num"), ("Per $", "num"), ("Resting", "num"), ("Holding", "num")], rows, "")


def scorecard_table(state):
    tot = (state.get("scorecard") or {}).get("totals") or {}
    rows = []
    for key, name in (("all", "All brackets"), ("high", "Daily highs"), ("low", "Daily lows")):
        t = tot.get(key)
        if not t or not t["n"]:
            rows.append(f'<tr><td>{name}</td><td class="num">0</td><td class="num">—</td><td class="num">—</td>'
                        f'<td class="num"><span class="pill flat">No data yet</span></td></tr>')
            continue
        mb, kb = t["model"] / t["n"], t["market"] / t["n"]
        if t["n"] < 200:
            verdict, cls = "Too early to tell", "flat"
        elif mb < kb * 0.97:
            verdict, cls = "Model more accurate", "up"
        elif mb > kb * 1.03:
            verdict, cls = "Market more accurate", "down"
        else:
            verdict, cls = "About even", "flat"
        rows.append(f'<tr><td>{name}</td><td class="num">{t["n"]:,}</td><td class="num">{mb:.4f}</td>'
                    f'<td class="num">{kb:.4f}</td><td class="num"><span class="pill {cls}">{verdict}</span></td></tr>')
    return r._table([("Markets", ""), ("Scored", "num"), ("Model error", "num"), ("Market error", "num"),
                     ("Verdict", "num")], rows, "")


def forecast_cards(run):
    evs = sorted(run.get("events") or [], key=lambda e: (e["date"], e["code"], e["kind"]))
    if not evs:
        return '<p class="muted">No weather markets modeled on the last run.</p>'
    cards = []
    for e in evs[:60]:
        s = e.get("summary") or {}
        rows = sorted(e.get("rows") or [], key=lambda x: -x["q"])[:4]
        mini = ""
        for x in rows:
            mkt = None
            if x.get("bid") is not None or x.get("ask") is not None:
                mkt = ((x.get("bid") or 0) + (x.get("ask") if x.get("ask") is not None else 1)) / 2
            mini += (f'<tr><td>{esc(x["label"])}</td>'
                     f'<td><span class="mbar" style="width:{x["q"] * 60:.0f}px"></span> {_pct(x["q"])}</td>'
                     f'<td><span class="mbar mk" style="width:{(mkt or 0) * 60:.0f}px"></span> {_pct(mkt)}</td></tr>')
        obs = f' · seen so far {e["observed"]:.0f}°' if e.get("observed") is not None else ""
        nbm = f' · NBM {e["nbm"]:.0f}°' if e.get("nbm") is not None else ""
        name = esc((e.get("name") or e["code"])[:28])
        cards.append(
            f'<div class="card"><b>{esc(e["code"])} {e["kind"]} · {e["date"][5:]}</b>'
            f'<div class="muted">{name}</div>'
            f'<div>Model {s.get("mean", "—")}° (80% range {s.get("p10", "—")}–{s.get("p90", "—")}°){nbm}{obs}</div>'
            f'<div class="muted">bias {e["bias"]:+.1f}° ({esc(e["bias_src"])}) · '
            f'{" + ".join(f"{k} {v}" for k, v in (e.get("members") or {}).items())} runs</div>'
            f'<table class="mini"><tr class="muted"><td>Bracket</td><td>Model</td><td>Market</td></tr>{mini}</table></div>')
    return f'<div class="cards">{"".join(cards)}</div>'


def health_table(run):
    h = run.get("health") or {}
    if not h:
        return '<p class="muted">No external data calls on the last run.</p>'
    rows = []
    for k, v in sorted(h.items()):
        cls = "down" if v["fail"] and not v["ok"] else ("flat" if v["fail"] else "up")
        rows.append(f'<tr><td>{esc(k)}</td><td class="num up">{v["ok"]}</td>'
                    f'<td class="num {cls}">{v["fail"]}</td><td class="mkt muted">{esc(v.get("last_error") or "")}</td></tr>')
    return r._table([("Source", ""), ("OK", "num"), ("Failed", "num"), ("Last error", "")], rows, "")


def weather_body(state, now_ts=None):
    now_ts = now_ts or time.time()
    if not state:
        return ('<header><div><h1>Weather bot <span class="badge">PAPER MONEY</span></h1>'
                '<div class="muted">Hasn\'t run yet. It starts on the next scheduled run.</div></div></header>')
    start = state["starting_bankroll"]
    mark, book = wb.mark_equity(state), wb.book_equity(state)
    run = state.get("last_run") or {}
    closed = state["closed"]
    wins = sum(1 for p in closed if p["pnl"] > 0)
    realized = sum(p["pnl"] for p in closed)
    held = sum(p["cost"] for p in state["positions"])
    res = wb.reserved(state)
    goal_pct = max(0.0, min(1.0, (book - start) / (wc.GOAL_EQUITY - start))) if wc.GOAL_EQUITY > start else 0
    ph = state.get("phase") or {}

    alerts = ""
    if run.get("halt"):
        alerts += f'<div class="alert">⏸ {esc(run["halt"])}</div>'
    if run.get("error"):
        alerts += f'<div class="alert err">Last run hit an error: {esc(run["error"])}</div>'
    bad = [k for k, v in (run.get("health") or {}).items() if v["fail"] and not v["ok"]]
    if bad:
        alerts += f'<div class="alert">Data source down on the last run: {esc(", ".join(bad))}. The bot skipped it.</div>'

    kpis = f"""
<div class="kpis">
  <div class="kpi"><div class="k">Account value</div><div class="v">{r.money(mark)}</div>
    <div class="s {r.tone(mark - start)}">{r.signed(mark - start)} ({(mark - start) / start:+.1%})</div></div>
  <div class="kpi"><div class="k">Goal: {r.money(wc.GOAL_EQUITY)}</div><div class="v">{goal_pct:.0%}</div>
    <div class="goalbar"><span style="width:{goal_pct * 100:.0f}%"></span></div>
    <div class="s">book value {r.money(book)}</div></div>
  <div class="kpi"><div class="k">Realized profit</div><div class="v {r.tone(realized)}">{r.signed(realized)}</div>
    <div class="s">{wins}–{len(closed) - wins} · {len(closed)} settled</div></div>
  <div class="kpi"><div class="k">In play</div><div class="v">{r.money(held + res)}</div>
    <div class="s">{len(state['positions'])} held ({r.money(held)}) · {len(state['orders'])} resting orders ({r.money(res)})</div></div>
</div>"""

    order_rows = []
    for o in sorted(state["orders"], key=lambda o: o["close_ts"]):
        order_rows.append(
            f'<tr><td class="mkt">{esc(o["label"])}</td><td><span class="side">{o["side"].upper()}</span></td>'
            f'<td>{wb.STRATEGY_NAMES.get(o["strategy"], o["strategy"])}</td>'
            f'<td class="num">{r.cents(o["price"])}</td><td class="num">{o["filled"]:g}/{o["contracts"]:g}</td>'
            f'<td class="num">{_pct(o.get("q_model"))}</td><td class="num">{r.fmt_in(now_ts - o["placed_ts"])}</td>'
            f'<td class="num">{r.fmt_in(o["close_ts"] - now_ts)}</td></tr>')
    orders_tbl = r._table([("Market", ""), ("Side", ""), ("Strategy", ""), ("Our bid", "num"),
                           ("Filled", "num"), ("Model", "num"), ("Resting for", "num"), ("Closes", "num")],
                          order_rows, "No resting orders right now.")

    pos_rows = []
    for p in sorted(state["positions"], key=lambda p: p["close_ts"]):
        unreal = p["contracts"] * p.get("mark_price", p["avg_price"]) - p["cost"]
        pos_rows.append(
            f'<tr><td class="mkt">{esc(p["label"])}</td><td><span class="side">{p["side"].upper()}</span></td>'
            f'<td>{wb.STRATEGY_NAMES.get(p["strategy"], p["strategy"])}</td><td class="num">{p["contracts"]:g}</td>'
            f'<td class="num">{r.cents(p["avg_price"])}</td><td class="num">{r.money(p["cost"])}</td>'
            f'<td class="num {r.tone(unreal)}">{r.signed(unreal)}</td>'
            f'<td class="num">{r.money(p["contracts"] - p["cost"])}</td><td class="num">{r.fmt_in(p["close_ts"] - now_ts)}</td></tr>')
    pos_tbl = r._table([("Market", ""), ("Side", ""), ("Strategy", ""), ("Qty", "num"), ("Paid", "num"),
                        ("Cost", "num"), ("If sold now", "num"), ("Profit if win", "num"), ("Closes", "num")],
                       pos_rows, "No filled positions yet. Maker orders can take a while to fill.")

    closed_rows = []
    for p in sorted(closed, key=lambda p: p["closed_ts"], reverse=True)[:25]:
        closed_rows.append(
            f'<tr><td class="mkt">{esc(p["label"])}</td><td><span class="side">{p["side"].upper()}</span></td>'
            f'<td>{wb.STRATEGY_NAMES.get(p["strategy"], p["strategy"])}</td><td class="num">{p["contracts"]:g}</td>'
            f'<td class="num">{r.cents(p["avg_price"])}</td>'
            f'<td class="num"><span class="pill {"up" if p["pnl"] > 0 else "down"}">{"WON" if p["pnl"] > 0 else "LOST"}</span></td>'
            f'<td class="num {r.tone(p["pnl"])}">{r.signed(p["pnl"])}</td><td class="num">{r.fmt_dt(p["closed_ts"])}</td></tr>')
    closed_tbl = r._table([("Market", ""), ("Side", ""), ("Strategy", ""), ("Qty", "num"), ("Paid", "num"),
                           ("Result", "num"), ("Profit", "num"), ("Settled", "num")],
                          closed_rows, "Nothing has settled yet. Weather markets settle the morning after.")

    acts = (f"Last run: {run.get('markets', 0)} weather markets · {len(run.get('events') or [])} city-days modeled · "
            f"{len(run.get('fills') or [])} fills · {len(run.get('placed') or [])} new orders · "
            f"{len(run.get('cancelled') or [])} cancelled/repriced · {run.get('seconds', 0)}s")
    cfg = (f"Maker-only: posts limit orders, never takes the ask. Fills count only when a real public trade "
           f"prints at our price or better (at-price prints fill {wc.FILL_AT_PRICE_SHARE:.0%}). "
           f"Half-Kelly sizing, max {wc.MAX_BET_FRACTION:.0%} per market, {wc.MAX_EVENT_FRACTION:.0%} per city-day, "
           f"{wc.MAX_TOTAL_EXPOSURE:.0%} total. Stops at {r.money(wc.GOAL_EQUITY)} (goal) or {r.money(wc.FLOOR_EQUITY)} (floor).")

    return f"""<header>
  <div><h1>Weather bot <span class="badge">PAPER MONEY</span></h1>
  <div class="muted">Imaginary {r.money(start)} · started {r.fmt_dt(state['created_ts'])} · {esc(ph.get('name', ''))}</div></div>
  <div class="muted">Last run {r.fmt_dt(run.get('ts'))}<br>Book value {r.money(book)}</div>
</header>
{alerts}
{kpis}
<section class="panel"><h2>Account value</h2>{r.equity_svg(state['equity_history'], start)}</section>
<section class="panel"><h2>Strategies</h2>
  <p class="muted" style="margin-top:-4px">Each strategy is scored on its own, so we can drop the losers.
  <b>Longshot NO</b> sells the cheap 1–4¢ brackets the evening before (the best-documented edge).
  <b>Favorite maker</b> bids on strong favorites the forecast agrees with. <b>Model edge</b> (experimental)
  bets where the forecast and the market disagree by 12+ points and both forecast models agree.</p>
  {strategy_rows(state)}</section>
<section class="panel"><h2>Is the forecast beating the market?</h2>
  <p class="muted" style="margin-top:-4px">For every bracket we model, the bot saves its forecast and the market price from
  the day before, then grades both once Kalshi settles (Brier score: lower = more accurate). If the market keeps
  winning this, trust the market and keep the model as a filter only.</p>
  {scorecard_table(state)}</section>
<section class="panel"><h2>Resting orders</h2>{orders_tbl}</section>
<section class="panel"><h2>Positions</h2>{pos_tbl}</section>
<section class="panel"><h2>Recent settled</h2>{closed_tbl}</section>
<section class="panel"><h2>Forecast board</h2>
  <p class="muted" style="margin-top:-4px">{esc(acts)}. Blue = our model's probability, gray = the market's.
  Forecasts: ECMWF (51 runs) + GFS (31 runs) ensembles and the National Blend of Models, over the official NWS
  climate day, corrected for each station's bias, cut off by today's observed readings.</p>
  {forecast_cards(run)}</section>
<section class="panel"><h2>Data sources (last run)</h2>{health_table(run)}</section>
<footer>{esc(cfg)}<br>Paper trading only; nothing is sent to Kalshi. Not financial advice.</footer>"""
