# Kalshi Paper Bot

A hands-off bot that trades Kalshi prediction markets with **imaginary money**
($100 to start), so you can see how a strategy performs before risking the real
$100. It reads live public market data — no Kalshi login or API key needed.

## The strategy in plain English

**"Favorite harvester."** A study of 300,000+ Kalshi contracts (Bürgi, Deng &
Whelan, *Makers and Takers: The Economics of the Kalshi Prediction Market*)
found a *favorite–longshot bias*: cheap longshots win far less often than their
price implies, while strong favorites win slightly *more* often and earn small
positive returns. Prices also get more accurate as markets near their close.

So every 15 minutes the bot:

1. Looks at markets closing in the next 15 min – 36 hours.
2. Keeps only liquid ones (≥200 contracts traded/day, spread ≤3¢) where one side
   costs **80–95¢**, and skips favorites whose price is sliding. All of these are
   tracked for the evidence table, but only **85–95¢** favorites are bet (week 1
   showed the 80–85¢ band losing after fees).
3. Ranks them by how trustworthy the price is (close to settlement, heavily
   traded, tight spread).
4. Buys the favorite with small, capped bets and holds until Kalshi settles it.

### The math you should know going in

At 90¢, a win pays about **10¢** and a loss costs about **90¢**. After Kalshi's
fee you need a **~91% hit rate just to break even**. Most days look great
(win, win, win…) and then one loss eats nine wins. That's normal for this
strategy — the question is whether the wins outrun the losses over hundreds of
bets. The bot is built to answer exactly that.

## Safety rails (all in `config.py`)

| Rule | Default | Why |
|---|---|---|
| Max per bet | 6% of account (week 1: 3%) | A loss costs ~6%; a win pays ~0.5% |
| Max in play | 90% of account (week 1: 60%) | Keeps idle cash working; 10% stays in reserve |
| Max open bets | 20 (week 1: 15) | |
| One bet per event | — | Related markets (e.g. temperature strikes) move together |
| Max 2 bets per series | — | No piling into one city / league |
| Max 1 bet per related group | — | CPI, Core CPI and CPI-YoY are separate series but one report decides them all. Groups (inflation, oil, crypto, Fed…) are keyword patterns in `config.py` |
| Daily stop | −15% (week 1: −8%) | Stops new bets for the rest of the UTC day |
| Drawdown brake | −35% from peak (week 1: −20%) | Stops new bets until you review |

**Experiment phases:** `PHASE_NAME` in `config.py` labels the current settings.
Change it whenever you change the strategy; the dashboard's **This phase** card
then scores the new settings on their own, and the chart marks when they began.
Brakes reset at the start of each phase.

Week 2 simulation (bets drawn from week 1's real prices): about **+8.5%** in a
week if week 1's edge was real, about **−3%** if prices are simply fair, and
roughly 1 week in 11 ending down 20%+ in the no-edge case.

## Shadow stop-loss (logging only)

The bot never sells early; every bet is held to settlement. To find out whether
a stop-loss *would* help, it records, for each bet, the first time the bid
falls 10¢, 15¢, 20¢ and 30¢ below what was paid, plus what selling right then
would have returned. The dashboard's **Shadow stop-loss** panel compares that
with what holding actually paid. Green = selling would have beaten holding.
Only bets opened after the feature was added are counted.

## Option A — run it on GitHub (recommended, fully hands-off)

You don't need your PC on. Same idea as your bowling-standings automation.

1. Create a **new private GitHub repo** (e.g. `kalshi-paper-bot`).
2. Push this folder to it:
   ```
   git init
   git add .
   git commit -m "Kalshi paper bot"
   git branch -M main
   git remote add origin https://github.com/<you>/kalshi-paper-bot.git
   git push -u origin main
   ```
3. On GitHub: **Actions** tab → enable workflows if asked → **Kalshi paper bot**
   → **Run workflow** to kick off the first cycle right away.
4. From then on it runs every ~15 minutes and commits its results.

**Viewing the dashboard:** `git pull`, then open `docs/index.html` in your
browser. (If you'd rather have a live URL, make the repo public and turn on
**Settings → Pages → Deploy from branch → main /docs**. The repo only holds
paper trades, nothing secret.)

**Cost:** ~670 runs/week × ~1 minute. Private repos get 2,000 free Actions
minutes a month, so a one-week test fits comfortably; a full month would not.

**If the push step fails with a 403:** Settings → Actions → General →
Workflow permissions → **Read and write permissions**.

## Option B — run it on your PC

Needs Python 3.10+. No packages to install.

```
python paper_bot.py               # one cycle (do this first to confirm it can reach Kalshi)
python paper_bot.py --loop 15     # keep running, every 15 min (PC must stay awake)
python paper_bot.py --report      # rebuild the dashboard from saved state
python paper_bot.py --reset       # back up the old run and start fresh at $100
```

Dashboard: `docs/index.html`. Every trade is also logged to
`state/trade_log.csv` (opens fine in Excel).

## Reading the results after a week

The dashboard's most important section is **"Is the edge real?"** The bot logs
*every* market that passed its filters — bet on or not — and grades it when it
settles. That gives you hundreds of data points instead of just your own bets.

Go live only if, after a week:

- The **All** row shows favorites winning more often than *Needed to break even*
  (positive edge), ideally with the verdict **"Beating the price"**.
- The paper account is up, and its worst dip was modest (under ~10%).
- The brakes didn't fire repeatedly.

If the verdict is **"Inconclusive so far,"** that's common — a 1–2 point edge
takes roughly 1,000+ graded markets to confirm. Run another week; it's free.
If it says **"Losing after fees,"** don't go live with this strategy.

## Test it offline

```
python -m unittest discover tests -v         # 20 tests: fees, filters, sizing, settlement, brakes
python tests/simulate_week.py                # a fake week where favorites have NO edge
python tests/simulate_week.py --edge 0.02    # a fake week where favorites win 2 pts more than priced
```

The simulator writes `sim_output/index.html`. With no edge, expect a loss
(fees) that the brakes cap around −20%. Its markets are random, not Kalshi's.

## Files

| File | What it does |
|---|---|
| `config.py` | Every setting. Tune here. |
| `kalshi_api.py` | Read-only client for Kalshi's public market data |
| `strategy.py` | Market filters, ranking, fee math, settlement reading |
| `portfolio.py` | The paper account: sizing, opening/settling bets, brakes |
| `report.py` | Builds the HTML dashboard |
| `paper_bot.py` | Runs one cycle (or loops) |
| `.github/workflows/paper-bot.yml` | The 15-minute GitHub schedule |

## Known simplifications

- Fills are simulated at the best ask with the general taker fee. Real fills
  can be slightly worse, and a few Kalshi products use a different fee schedule.
- Bets are held to settlement; there's no early selling yet.
- Real-money trading (signed API keys, real orders) is a separate, later step.

*Not financial advice. It's a robot with imaginary money.*
