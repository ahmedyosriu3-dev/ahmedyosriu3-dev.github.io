# Trading Desk

A private dashboard that watches a list of US stocks and ETFs, decides which
technical strategy suits each one, and tells you when its rules fire. For
symbols you explicitly allow, it queues an order for you to approve with one
click — on the desk, or from Telegram on your phone.

It also goes looking. Once a week the **idea scout** screens a few hundred
stocks, ETFs, bond funds and commodities and suggests what might deserve a
look, ranked partly on how little it moves with what you already own.

It runs entirely on your own machine.

---

## What it actually does

1. Pulls daily bars for your watchlist and caches them in SQLite.
2. Runs five strategies over each symbol's history in a **walk-forward** test,
   scoring each one **out-of-sample only**.
3. Assigns the winner to that symbol — or marks it `NO EDGE` and trades nothing,
   which is the honest answer more often than not.
4. Each scheduled scan, the assigned strategy produces BUY / SELL / HOLD with a
   plain-English reason.
5. Depending on the symbol's mode, it either just tells you, or sizes a
   risk-checked order and waits for your approval.
6. It tells you about all of it on Telegram, and — if you switch that on —
   lets you approve from there.
7. Weekly, it screens a wider universe for instruments worth researching.

### What it does not do

It does not predict prices. It does not find alpha. A good backtest is evidence
that a rule *would have* worked on data you already have, which is a much
weaker claim than it feels like. Most strategies lose to buying and holding —
the app shows you that comparison on every backtest rather than hiding it.

---

## Quick start

```bash
cd "E:\My_apps\Finnancial tool"
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000>.

With no Alpaca keys, the app runs against a **local simulator** with $100,000 of
pretend money, so every screen works before you open any account.

### First run

1. **Settings → Watchlist** — add a few symbols.
2. **Run strategy selection** — walk-forward evaluation, a few seconds per symbol.
3. **Overview → Rescan signals** — generates today's readings.

---

## Per-symbol modes

Set independently for every symbol, with its own money:

| Mode | Behaviour |
|---|---|
| `OFF` | Watched, shown, silent. Never signals. |
| `SIGNAL_ONLY` | Tells you what it sees. Never creates an order. |
| `SEMI_AUTO` | Sizes a risk-checked order and queues it for your approval. |

`allocation_usd` caps how much money a symbol may ever hold. `risk_pct` is how
much of that allocation is risked per trade, which drives ATR position sizing.

Nothing is ever sent to a broker without you pressing **Approve**.

---

## Credentials

Every key the app takes lives in `.env`, and **Settings -> Credentials** writes
that file for you -- paste a key, press Save, and the broker, bot or analyst
picks it up without a restart.

A few deliberate details:

* A **blank box changes nothing**, so you can save one key without re-entering
  the others. Clearing a value is a separate tick, because an accidental empty
  save that silently disconnected your broker would be a bad way to find out.
* Saved secrets are shown only as their **last four characters**. The value
  itself is never sent back to the page, so it cannot end up in a screenshot.
* The audit log records *which* keys changed and never what they became.
* **Arming live trading is not on the form.** `ALPACA_LIVE=true` stays a manual
  edit -- a lock you can flip from the same screen as everything else is not
  much of a lock.
* If a key is also exported as a real environment variable, that wins over
  `.env` in pydantic-settings. The page says so rather than letting a saved
  value quietly vanish on the next restart.

`.env` is plain text and git-ignored. It is a password file; treat it as one.

You can still edit it by hand -- `.env.example` documents every key, and the
Settings form preserves the comments and ordering in whatever is already there.

---

## Connecting Alpaca

1. Create a free account at <https://app.alpaca.markets>.
2. **Paper Trading → API Keys** → generate a key.
3. Paste them into **Settings → Credentials**, or put them in `.env` yourself:

```ini
ALPACA_API_KEY=your_key
ALPACA_API_SECRET=your_secret
ALPACA_LIVE=false
```

4. The sidebar will show **Alpaca paper** instead of **Simulator**. Saving
   from Settings swaps the broker immediately; a hand-edited `.env` needs a
   restart.

Paper and live share one code path — only the base URL changes — so what you
validate on paper is exactly what would run live.

### Going live (the triple lock)

Three independent things must all be true before real money can move:

1. `ALPACA_LIVE=true` in `.env`
2. The **Live trading** toggle armed in Settings
3. A typed confirmation on the toggle

A fresh install cannot trade live by accident. Run on paper for weeks first and
compare what actually happened against what the backtest promised.

---

## Safety rails

Enforced in `app/trading/risk.py`, before anything reaches a broker:

- **ATR position sizing** — fixed dollars at risk; the stop distance sets the share count
- **Allocation cap** per symbol
- **Max open positions** (default 10)
- **Max deployed capital** (default 80% of equity)
- **Daily loss limit** (default 3%) → trips the **kill switch**, blocking all new orders
- **Cooldown** after a stop-out (default 3 days) — no revenge re-entry
- **Every entry carries a stop-loss.** Bracket orders only, no exceptions
- **Proposals expire** (default 2h) rather than going stale
- The gate **fails closed** — if a limit can't be evaluated, the answer is no

---

## Optional AI analyst

Commentary on a symbol from a local Llama model or Google Gemini. In `.env`:

```ini
# Local — nothing leaves your machine
AI_PROVIDER=ollama
OLLAMA_MODEL=llama3.1

# Or Google's API — sends your data summary to Google
AI_PROVIDER=gemini
GEMINI_API_KEY=your_key
GEMINI_MODEL=gemini-2.0-flash
```

For the local option, install <https://ollama.com> and `ollama pull llama3.1`.

**The model is deliberately outside the order path.** It is handed numbers the
app already computed and asked to interpret them — it cannot place, size, or
approve a trade, and no strategy or risk decision reads its output. Two tests
(`tests/test_ai.py`) enforce that separation structurally in both directions.

Its output is escaped before rendering, and every analysis ships with the exact
briefing it was given so you can check any figure it quotes.

---

---

## Symbol logos

Every ticker in the app carries its company (or fund) logo, so a row is
findable by shape before you have read a word of it.

How it works:

* Pages point at this app's own `/logo/<TICKER>` route — **your browser never
  talks to a logo provider**, so your watchlist is not broadcast to a third
  party and a slow provider cannot slow a page down twice.
* The server fetches each logo once, stores it under `data/logos/`, and serves
  it from disk from then on.
* A symbol with no artwork anywhere — most bonds, commodity trusts, the odd
  ticker — gets a coloured monogram tile drawn locally. The colour is derived
  from the ticker, so a symbol always looks the same.
* Failed lookups are cached for a week, so a missing symbol costs one request,
  not one per page view. Offline, everything falls back to monograms.

Using it well:

* Leave it alone. The cache warms itself as you browse and then costs nothing.
* Press **Refresh logos** in Settings after a rebrand, or once if the first
  fetch happened while you were offline — that is what clears the week-long
  misses.
* **A logo is decoration.** It is matched on ticker alone, and tickers get
  reused. Never use it to confirm *which* company you are buying; read the
  ticker and the name.
* `LOGOS_ENABLED=false` in `.env` stops every outbound request; all symbols
  then show monograms. `LOGO_URL_TEMPLATE` swaps the provider.

## Getting things done quickly

**Ctrl+K** opens a command palette from any page. Type a few letters of a
ticker to jump to it, or a few letters of a verb to run it — *rescan*, *scout*,
*selection*, *kill switch*. Nothing in this app should require remembering
which page its button lives on.

Single keys, when you are not typing in a field:

| Key | |
|---|---|
| `Ctrl K` or `/` | Command palette |
| `g` | Overview |
| `s` | Idea scout |
| `p` | Positions |
| `r` | Rescan signals |
| `?` | Shortcut list |

### The brief

The overview opens with one sentence saying whether anything needs you, and
usually it says no. Under it, the watchlist shows price, a 30-day sparkline,
the signal, and what the strategy is thinking — the columns you read daily.
Strategy internals, agreement, mode and allocation sit behind the **Detail**
toggle, which remembers whether you left it open.

### Buying something the strategies did not suggest

`Propose a buy now`, on the symbol page and in the watchlist, sizes an entry on
your own judgement: stop 2×ATR below the last close, target 3×ATR above,
through the identical risk gate, still ending in a proposal you approve. Your
own read is a legitimate source of a trade — leaving the app to act on it, and
ending up with an unprotected position placed somewhere else, is not.

Only available on `SEMI_AUTO` symbols, because allocation and risk budget are
only defined there.

---

## Telegram

The desk reaches you wherever you are, and — if you let it — takes instructions
back.

### Setup

1. Message [@BotFather](https://t.me/botfather), send `/newbot`, copy the token.
2. Paste it into **Settings → Credentials** as the bot token and save.
3. Message your new bot anything.
4. **Settings → Telegram → Find my chat id**, then save that as the chat id.

The bot restarts on save, so neither step needs a restart.

### What it sends

| When | What |
|---|---|
| 08:30 ET weekdays | Morning brief: equity, book, anything awaiting approval, fresh signals |
| On a proposal | The sized order, its stop, target and dollars at risk — with buttons |
| On a signal-only BUY/SELL | Quiet alert, no order |
| On submission | Confirmation with the broker order id |
| Kill switch trips | Immediately, loudly |
| Saturday 10:00 ET | The scout report |

### What it answers

`/status` `/brief` `/pending` `/positions` `/scan` `/scout` `/ideas`
`/watch SYM` `/sym SYM` `/kill` `/unkill` `/help`

### Approving from your phone

Off by default. `TELEGRAM_ALLOW_APPROVALS=true` turns the Approve button from
decoration into a button that submits; `TELEGRAM_ALLOW_LIVE_APPROVALS=true`
extends that to real money.

What protects you:

- Updates from any chat id other than yours are dropped and logged.
- A tap runs `engine.approve_proposal()` — the same function the dashboard
  button calls — so every risk check, the kill switch, the position limit and
  the cooldown all still apply. The bot cannot reach a broker directly, and
  two tests enforce that structurally.
- Live orders need a second confirming tap.
- Proposals still expire.

What does not protect you: **anyone holding your unlocked phone can press it,
and so can anyone who learns the bot token.** It is a bearer credential sitting
in a plaintext `.env`. If you lose the phone, revoke the token in BotFather.
Leaving live approvals off and approving real money on the desk costs you a few
minutes and removes this entire class of problem.

---

## Idea scout

Most weeks you should hold what you already hold. Occasionally it is worth
asking what else exists. The scout screens a curated, auditable universe —
~150 US large caps, 40 index/sector/factor ETFs, 29 bond and income funds, 18
commodity funds — and ranks them on five components, each shown so you can see
which one earned the ranking:

| Component | What it measures |
|---|---|
| **Trend** | A checklist: above the 200-day, 50 above 200, 200-day rising, above the 50-day |
| **Momentum** | Risk-adjusted 1/3/6/12-month return, ranked against everything else screened |
| **Steadiness** | Low volatility, shallow drawdowns, real liquidity |
| **Diversifies you** | How little it moves with what you *already own* |
| **Pullback** | Distance below the 52-week high — counted only while the trend is intact |

Weights differ by asset class. A bond fund earns its place by being
uncorrelated and steady; it is not asked to outrun the S&P.

**The column worth your attention is _diversifies you_.** Every other measure is
about the market and every screener has them. That one is about your portfolio,
it is computed against your actual watchlist and positions, and it is the thing
you cannot eyeball. A great instrument that moves exactly like something you
already own does not improve anything.

The page also reports a **market regime** — risk-on, choppy or defensive, read
from the benchmark's own trend and volatility — and tilts the weights with it.
In a defensive tape bonds and commodities are rewarded and high-volatility
equities docked, because most long strategies do badly there.

### Honest limits

- It has **no view on earnings, valuation, management, or news.** It reads
  price and volume. That is a real limitation, not a modest one.
- A high score is not a prediction. It describes how something looks now.
- Cash equivalents (T-bill funds) are capped and labelled as cash rather than
  ranked as ideas — an instrument that never falls maxes out trend and
  steadiness by construction and would otherwise top the list every week while
  telling you nothing.
- Something 60% off its high with a broken trend scores zero on pullback.
  Cheap and falling is not value.
- **Screening is not an edge.** Add an idea to the watchlist and run strategy
  selection. If nothing clears the bar out-of-sample the app says so, and
  buy-and-hold may well be the better answer.

Ideas arrive in `SIGNAL_ONLY` mode with no allocation. Giving anything money is
a separate, deliberate act.

Runs Saturdays at 10:00 ET, or on demand. A full run takes about two minutes
on a cold cache and seconds afterwards.

## Suggestions

The overview lists every watched symbol and what its strategy says, which
answers *what is the state of everything?* With thirty symbols that is a
different question from *what, if anything, should I do?* — and the second one
is why you opened the app. **Suggestions** has two tabs, each sorted strongest
first:

| Tab | What is in it |
|---|---|
| **Buy** | Every watched symbol whose assigned strategy is firing a buy today |
| **Sell from holdings** | Sell signals **only** on symbols you actually hold |

The sell tab is restricted on purpose. There is no short side in this app, so
a sell signal on something you do not own is not an instruction — listing it
would be noise at best and an invitation to do something dangerous at worst.

### What the conviction number is

A weighted blend of four things the app already knows, each shown as its own
bar on the card so a high score can be argued with rather than trusted:

| Component | Weight | What it means |
|---|---|---|
| **Demonstrated edge** | 35% | The out-of-sample score the selector gave this symbol's assigned strategy |
| **Strategies agreeing** | 35% | How many of the other strategies independently want the same side today |
| **Trend behind it** | 20% | Whether the 200/50-day picture supports the signal |
| **Freshness** | 10% | How recently it fired — a buy from six weeks ago is history, not a suggestion |

Agreement and trend are read the *other way round* for a sell: four of five
strategies wanting to be long is strong support for a buy and strong evidence
against selling.

Symbols the selector marked **NO EDGE** never appear, and neither do symbols in
`OFF` mode. An empty list is the normal answer on most days; a tool that always
finds something to do is a tool that is eventually always wrong.

**Ranking is not permission.** The order says which signal has the most
evidence behind it, not that it will work. Nothing is ordered without the
symbol's mode allowing it and you pressing Approve.

---

## Dogs of the Dow

The oldest mechanical strategy that still has a following, and the simplest:
on the last trading day of the year, rank the thirty Dow components by dividend
yield, buy the top ten equally weighted, hold for the whole of the next
calendar year, and do nothing else. The five cheapest of those ten by share
price are the **Small Dogs**.

The page shows the current list, how each name has done since the ranking date
with dividends counted, and the same numbers for the Dow itself so the
comparison is unavoidable.

### How the list stays current

It rebuilds on the first weekday morning in January that the app is running —
the job is scheduled across 2-15 January so a machine that was switched off on
the 2nd still picks it up — and then **does not change for the rest of the
year**. Rebuilding mid-year would be a different strategy wearing the same
name. There is a Rebuild button for when you want to force it.

Dividends come from Alpaca's corporate-actions endpoint using the API key
already in `.env`, falling back to yfinance without one. The yield uses the
**indicated annual dividend** — the latest regular payment times its frequency
— which is the convention the strategy has always used and what a buyer today
would actually collect.

### The things that quietly go wrong here

- **The ex-date is not the pay date.** Alpaca filters on the process date, so
  a dividend that goes ex in mid-December and pays in January is missing from
  a query ending on 31 December. Rank on that and a third of the Dow appears
  to have just raised its dividend by a third. Queries are widened and then
  filtered on the ex-date in `app/data/dividends.py`.
- **A suspended dividend is not a yield.** Boeing's last cheque went ex in
  February 2020 and the dividend was cut the following month; multiplying it
  by four would have carried a dead 3.8% yield into the end-of-2020 ranking
  and put a company paying nothing into the top ten. When a scheduled payment
  does not arrive, the app stops annualising and says so on the row.
- **Special dividends are excluded.** A one-off payout is not a rate.
- **Index membership is data, not a feed.** `app/analysis/dow.py` records the
  current thirty and the dated changes behind them, so a past year's list is
  built from the index as it stood then. It refuses to guess before August
  2020 rather than produce a confident wrong answer.

Reconstructed against the published lists, this reproduces 2022, 2023 and 2025
exactly, and 2021 and 2024 with the same ten names in a slightly different
order.

### What it is not

A value tilt with a good story. It trailed the index through most of the
2010s, and a yield is sometimes high because the dividend is about to be cut —
which is the one thing the screen cannot see. Nothing on the page places,
sizes or proposes an order.

---

## Pattern analysis

`app/analysis/patterns.py` answers a question people ask constantly and rarely
test: *does this thing actually repeat itself?*

```bash
.venv\Scripts\python.exe scripts\pattern_scan.py
.venv\Scripts\python.exe scripts\pattern_scan.py SPY GLD USO
```

| Measure | What it answers |
|---|---|
| **Variance ratio** | Do moves reverse or persist? Below 1 reverses, above 1 persists — with the Lo-MacKinlay heteroskedasticity-robust z-score, so a ratio of 0.97 cannot pose as a finding |
| **Dip and recovery** | How often it fell 10% or 20% below its 52-week high, how often it regained that level within a year, and how long that took |
| **Dip edge** | Forward six-month return when 10% off the high, against every other time |
| **Seasonality** | Month-by-month averages with the hit rate beside them |

Every measure is also computed on each half of the history separately. A
pattern that only exists in one half is a pattern you found by looking, and
that is what the `both` column reports.

The robust z-score is not optional decoration. Under the simpler homoskedastic
statistic every asset with volatility clustering — which is every asset — reads
as significantly mean reverting, and the module would print exciting findings
for a coin toss. `tests/test_patterns.py` feeds it a random walk, a built
mean-reverter and a built trender and requires the right answer for each.

Two caveats no arithmetic removes: the forward-return measures use overlapping
windows, so their effective sample is far smaller than the row count suggests
and no significance is claimed for them; and these are today's well-known
names, which is exactly the set that survived.

---

## Strategies

| Key | What it looks for |
|---|---|
| `ma_cross` | Fast EMA crosses above slow EMA, confirmed by ADX trend strength |
| `rsi_reversion` | Oversold RSI bounce, but only above the long-term average |
| `macd_trend` | MACD crosses its signal line in a healthy price regime |
| `bollinger` | Band breakout, or mean reversion — the selector picks which |
| `momentum` | Positive trailing return while above the trend average |

Adding a sixth is one file in `app/analysis/strategies/` — subclass `Strategy`,
decorate with `@register`, done.

### How the selector avoids fooling itself

- Expanding-window folds; parameters tuned in-sample, **scored out-of-sample only**
- Score = Sharpe − 2×drawdown + 0.15×profit factor, then docked for
  fold-to-fold inconsistency
- Configurations with fewer than 12 out-of-sample trades are **rejected as noise**
- If nothing clears the bar → `NO EDGE`, and the symbol trades nothing

---

## Testing

```bash
.venv\Scripts\python.exe -m pytest tests/ -q
```

139 tests. The ones that matter most:

- **Lookahead audit** — delaying every signal by one bar must change results;
  if it doesn't, the engine is peeking at the future and nothing else is trustworthy
- **Next-bar execution** — a price spike on the signal bar must not be captured
- **Stop semantics** — fills at the stop level, at the *open* on a gap, and when
  stop and target are both touched in one bar the **stop** wins (pessimistic)
- **No phantom re-entry** — a stopped-out position waits for a fresh signal
  instead of re-buying every bar
- **Risk sizing** — every cap and every refusal case
- **AI isolation** — the analyst cannot reach a broker; the order path cannot
  reach the analyst
- **Telegram isolation** — an unauthorised chat cannot reach the approval path;
  the bot cannot call a broker directly; live approvals need two taps
- **Scout honesty** — a cash-equivalent cannot top the rankings, a falling
  knife cannot score as value, a correlated idea is flagged as one

---

## Layout

```
app/
  certs.py              TLS trust bootstrap (for TLS-intercepting proxies/AV)
  envfile.py            reads/writes .env in place, comments preserved
  credentials.py        the Settings credential form, masked and audited
  config.py  db.py  models.py
  data/                 market data sources + bar cache
                        logos.py -- logo fetch/cache + monogram fallback
                        dividends.py -- cash dividends, ex-date aware
  analysis/             indicators, strategies, backtester, selector
                        universe.py + scout.py -- idea discovery
                        conviction.py -- ranks today's signals buy/sell
                        dow.py -- Dogs of the Dow, membership + ranking
                        patterns.py -- does this thing actually repeat?
  trading/              broker adapters, risk gate, decision engine
  ai/                   optional analyst (isolated from trading)
  notify/               telegram client, message composers, command bot
  templates/ static/    the dashboard
  jobs.py               scheduled tasks
scripts/
  pattern_scan.py       repeatability screen over well-known instruments
tests/
```

### Scheduled jobs (US/Eastern)

| Job | When |
|---|---|
| Refresh bars | every 30 min, market hours |
| Scan signals | 09:45 and 15:45 |
| Re-run strategy selection | Sundays 18:00 |
| Expire stale proposals | every 5 min |
| Sync account | every 15 min, market hours |
| Reset daily limits | 09:00 |
| Morning brief to Telegram | 08:30, weekdays |
| Idea scout + report | Saturdays 10:00 |
| Rebuild Dogs of the Dow | weekday mornings, 2-15 January |

---

## Notes

**TLS.** This machine sits behind something that re-signs HTTPS traffic, so
`certifi`'s roots aren't enough. `app/certs.py` merges the Windows trust store
into a bundle at startup and points every HTTP stack at it, including
`curl_cffi` (which yfinance uses and which ignores Python's `ssl` module).
Harmless on machines without interception.

**Data.** yfinance by default; Alpaca's free IEX feed once keys are present.
Backtests use adjusted closes so splits don't create phantom signals.

Alpaca's free plan serves IEX equities but **refuses SIP data**, which is every
ETF. `get_bars()` falls back to yfinance whenever a source returns nothing, so
adding a broker key does not silently remove ETFs, bond funds and commodities
from the scout, the benchmark and every backtest. A fetch that fails is also no
longer recorded as a backfill attempt — doing so capped a symbol's history at
whatever was already cached, permanently.

**This is not investment advice.** It is a tool that executes rules you chose,
with guardrails to limit what a bug or a bad day can cost you. The judgement
stays yours.
