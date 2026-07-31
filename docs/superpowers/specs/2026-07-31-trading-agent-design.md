# Trading Agent — Design

**Date:** 2026-07-31
**Status:** Approved
**Branch:** `feat/trading-agent`

An autonomous paper-trading agent for Claub: a `trader` agent with its own Discord
channel that researches the market daily, places trades against an Alpaca paper
account through a purpose-built MCP server with hard safety rails, journals every
decision, and scores itself against honest benchmarks. Designed to flip from paper
to real money with a key swap once (if) it earns trust.

## Goals and non-goals

**Goals**

- A trading agent whose "smartness" comes from the model plus the existing Claub
  harness (scheduler, Playwright browser, WebSearch/WebFetch) — not from a
  strategy framework.
- Paper money first, with an evaluation harness rigorous enough to tell whether
  the agent adds anything over buy-and-hold.
- Low running cost: free paper account, free market data, one daily session.
- Vendor-swappable: the broker integration is one thin adapter file, not a
  framework dependency.

**Non-goals**

- Intraday/high-frequency trading (cost-dominated; no LLM edge in the evidence).
- Options, shorting, margin, crypto (v1 is long-only US stocks and ETFs).
- Multi-agent debate/committee architectures (evidence runs negative).
- Beating the market. The honest goal is a well-instrumented experiment.

## Evidence base (what shaped this design)

Full research reports are in the session record; headline findings:

1. **No published end-to-end LLM trading agent has demonstrated statistically
   significant alpha under fair evaluation** (FINSABER, KDD 2026: over 2004-2024
   and 63-91 delisting-inclusive symbols, buy-and-hold beats FinMem at p=3e-6;
   all CAPM-alpha p-values > 0.34 — with training-data leakage still favoring
   the agents). Live benchmarks (AI-Trader, StockBench, Agent Market Arena)
   concur. Expectations are set accordingly.
2. **Component ablations rank what helps** (internal comparisons, so leakage
   largely cancels):
   - *Hard computed risk limits that can veto the LLM* — largest measured effect
     in the literature (FinCon's CVaR ablation: one asset flips −52.9% → +17.5%,
     MDD 70% → 41%). Static "be cautious" personas lose money in both directions.
   - *Reflection anchored to realized P&L, distilled and persisted* — +33-73%
     Sharpe in the cleanest ablation (FinAgent); FinCon's belief-update flips a
     losing asset positive. Must anchor to actual P&L, not self-judged quality.
   - *News/text analysis over price-only prompting* — 52% → 63% directional
     accuracy (LLMFactor); replicated in three other papers. Text is where the
     LLM edge lives.
   - *Skip:* multi-agent debate (wins <20% of 36 configurations; adding rounds
     hurts), short-horizon technical timing (10-20 bps round-trips compound to
     25-50 points of annual drag at daily turnover).
3. **Horizon verdict:** days-to-weeks holding periods, low turnover, decisions
   driven by news and fundamentals.
4. **Live paper trading is inherently post-knowledge-cutoff** — the clean
   out-of-sample test the literature lacks. Any edge observed here is real in a
   way backtests cannot show.
5. **Broker:** Alpaca. Free paper trading (no KYC), commission-free US equities,
   `paper=True|False` switch, healthy official SDK (`alpaca-py`), free SIP
   historical daily bars (end must be ≥15 min old — invisible at daily cadence).
   Note: the PDT rule was retired and Alpaca removed all PDT API fields on
   2026-07-06; no PDT handling code should exist. Paper fills are optimistic (no
   slippage/market impact), so paper validates *logic*, not *edge*.

## Architecture

```
Discord #trader channel
    │
    ▼
trader AgentProcess (claude CLI, opus)
    │
    ├─ mcp__alpaca__* ──► mcps/alpaca/ FastMCP server (new, baked into image)
    │                        │  hard rails enforced in code
    │                        ▼
    │                     broker.py interface ──► alpaca_impl.py (alpaca-py SDK)
    │                        │
    │                        ▼
    │                     Alpaca paper API (paper-api.alpaca.markets)
    │
    ├─ mcp__playwright__* ──► host bridge (news sites needing JS)
    ├─ WebSearch / WebFetch ──► news, filings, earnings
    ├─ mcp__schedules__* ──► daily run + weekly reflection
    └─ workspace: journal, beliefs.md, memory/
```

One new component: the `mcps/alpaca/` MCP server. Everything else is existing
Claub machinery plus configuration.

## The alpaca MCP server (`mcps/alpaca/`)

Follows the house pattern (hass-style narrow typed wrappers; `build-mcp-server`
skill). Hand-written tool descriptions — they are the agent's UI and carry
operational caveats ("check market_clock before assuming this fills today").

### Layout

```
mcps/alpaca/
  pyproject.toml          # deps: fastmcp, alpaca-py, httpx
  server.py               # FastMCP tools; rails checked here
  broker.py               # Broker protocol (dataclasses + abstract interface)
  alpaca_impl.py          # the only file that imports alpaca-py
  rails.py                # pure functions: every rail check, unit-testable
  performance.py          # benchmark math for get_performance_report
  tests/
```

### Tools (~12)

| Tool | Notes |
|---|---|
| `get_account` | equity, cash, buying power |
| `get_positions` | open positions with unrealized P&L |
| `get_quote(symbol)` | latest quote; description warns of 15-min staleness on free tier |
| `get_daily_bars(symbol, start, end)` | historical daily OHLCV |
| `get_market_clock` | open/close state, next open |
| `place_order(symbol, side, qty or notional, type, limit_price?, stop_loss?, take_profit?)` | market/limit; optional bracket legs; all rails checked here |
| `cancel_order(order_id)` | |
| `list_open_orders` | |
| `get_order(order_id)` | status + fill price |
| `get_portfolio_history(period)` | equity curve from Alpaca |
| `get_performance_report(period)` | computed scoreboard — see Evaluation |
| `get_rails` | echoes current rail config + remaining order budget today, so the agent can plan without tripping rejections |

### Hard rails (enforced in `server.py`/`rails.py`, not prompt)

| Rail | Default | Mechanism |
|---|---|---|
| Long-only | always | `sell` allowed only up to current position qty; no short sells |
| Asset universe | US common stock + ETF, tradable+marginable per Alpaca asset metadata | checked per order |
| Max position size | 10% of account equity per symbol (at order time) | order rejected with explanatory error |
| Max orders per day | 3 | counted per calendar day (ET); state in a small JSON file in the server's data dir |
| Drawdown circuit breaker | equity < 85% of high-water mark → buys blocked, sells allowed | high-water mark persisted; checked per order |
| Kill switch | `ALPACA_TRADING_DISABLED=1` blocks all order placement | env var |
| Paper guard | server refuses to start if `ALPACA_PAPER != "true"` unless `ALPACA_LIVE_CONFIRMED=I_UNDERSTAND` is also set | startup check |

Rejected orders return a clear reason string (e.g. "rejected by rail
max_position_pct: order would make AAPL 14% of equity; cap is 10%") so the agent
can adapt instead of retrying blindly.

Rail limits are env-configurable (`ALPACA_MAX_POSITION_PCT`,
`ALPACA_MAX_ORDERS_PER_DAY`, `ALPACA_DRAWDOWN_HALT_PCT`) with the defaults
above, set in docker-compose from `.envrc`. They are the *hard ceiling*; the
agent's workspace config may hold tighter self-imposed limits but can never
loosen these.

### Broker abstraction

`broker.py` defines a small protocol (dataclasses for Account, Position, Quote,
Bar, Order + an abstract `Broker` class with the ~10 operations above).
`alpaca_impl.py` is the only file importing `alpaca-py`. Swapping brokers later
means writing one new impl file and changing an env var. No Lumibot or other
strategy framework — inverted control is the wrong shape for a tool-calling
agent.

## Evaluation: `get_performance_report`

The scoreboard is arithmetic computed in `performance.py`, never LLM
self-assessment. For a requested period it reports:

- Agent: total return, annualized vol, Sharpe (using a real risk-free rate from
  a configurable constant, default 4%), max drawdown — from Alpaca portfolio
  history.
- **Benchmark 1 — SPY total return** over the same period (computed from
  adjusted daily bars).
- **Benchmark 2 — buy-and-hold of what the agent actually traded**: take the
  agent's first-acquisition weights and hold them; approximated from the trade
  journal's recorded entries (the MCP reads the journal file the agent
  maintains; if absent, falls back to current-position weights with a caveat in
  the output).
- Delta vs both benchmarks, stated plainly.

The tool's description instructs the agent to include this report in its weekly
reflection post, so Discord gets an honest scoreboard on cadence. The evidence
review showed benchmark selection is where this entire literature fooled itself;
benchmark math therefore lives in code.

## Claub wiring

- **agents.yaml**: `trader` entry, `channel_id: "YOUR_TRADER_CHANNEL_ID"`,
  `display_name: "Trader"`, `allowed_tools_additional: ["mcp__alpaca__*"]`.
- **settings.json**: add `mcp__alpaca__*` to the allow list (hard ceiling).
- **Per-agent MCP config** `agents/trader.mcp.json`: playwright bridge + alpaca
  server (stdio, `uv run` from `/app/mcps/alpaca`, env passed through).
- **Playwright bridge**: add a `trader` browser profile on the host
  (per `claub-playwright` skill) so news sites needing JS are reachable.
- **Env**: `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (paper keys) + `ALPACA_PAPER=true`
  in `.envrc` → docker-compose environment. Live keys stay out of the container
  entirely until a deliberate flip.
- **Entrypoint**: no changes — `/app/mcps/` servers are already uv-synced.

### Agent identity (Level 2, `config/agents/trader.md`)

Stable core only:

- Role: disciplined portfolio manager running a long-only US equity paper
  account. Temperament: patient, skeptical of hype, comfortable doing nothing.
  "No trade" is a valid, common outcome of a session.
- Decision procedure per session: read `beliefs.md` and open-position theses →
  check account, positions, market clock, performance vs benchmark → research
  (news for held names first, then watchlist/new ideas; prefer primary sources;
  markets move on surprises, not olds) → for any trade, write the structured
  thesis *before* placing the order → place order → journal it.
- Structured thesis schema (journaled per trade): symbol, direction, size
  rationale, thesis (2-4 sentences), catalyst/horizon (days-weeks), exit
  condition (both profit case and invalidation case), and — on close — realized
  P&L vs thesis, filled in by the weekly reflection.
- Journaling protocol and memory structure (below), reference to workspace
  CLAUDE.md for tunable strategy parameters.
- Honesty rules: cite the computed performance report, never self-estimated
  performance; paper fills are optimistic — treat results as logic-validation.
- Discord style: concise; a daily post only when something happened or something
  is worth saying (journalist-style silence discipline); weekly reflection post
  always, with the scoreboard.

### Workspace living config (Level 3, `workspaces/trader/CLAUDE.md`)

Seeded at first deploy; agent self-modifies on request:

- Watchlist / universe focus (seed: liquid S&P 500 names + broad ETFs).
- Self-imposed limits at-or-below the hard rails (target 5-10 positions,
  soft max 2 orders/day, min holding intent ~5 trading days).
- Current strategy focus and constraints the user sets conversationally.

### Memory (`workspaces/trader/memory/`)

```
memory/
  index.md              # required startup read (global rule)
  beliefs.md            # distilled lessons from realized P&L; the reflection
                        #   run maintains this; bounded (~20 beliefs max, each
                        #   cites the trades that earned it)
  journal/
    2026-08.md          # one file per month; structured thesis entries,
                        #   appended at trade time, closed out at reflection
  performance/
    weekly.md           # rolling log of weekly scoreboard snapshots
```

Retention: journal files kept indefinitely (they are the dataset); beliefs
pruned/merged at each reflection (write-time pruning per house rules).

### Schedules (created at runtime via `mcp__schedules__*`)

- **Daily session**: weekdays 10:30 ET (`30 10 * * 1-5`) — after the open so
  overnight news and the opening auction have settled; the agent checks the
  market clock and exits early on holidays. 5 firings/week.
- **Weekly reflection**: Friday 17:00 ET (`0 17 * * 5`) — market closed; review
  the week's fills and closed positions, compute the performance report, update
  `beliefs.md` and close out journal entries, post the scoreboard to Discord.
  1 firing/week.

Current global schedule load (journalist daily + one-shot chains) leaves ample
headroom under the 5/24h and 30/7d density limits.

## Error handling

- **Alpaca API down / auth failure**: tools return the error verbatim; agent
  reports in Discord and skips the session rather than retrying aggressively.
- **Order rejected by rails**: explanatory string; agent may resize once or skip.
- **Order rejected by Alpaca** (e.g. market closed, insufficient buying power):
  surfaced verbatim; the market-clock tool exists so this is rare.
- **Rail state file corrupt/missing**: fail closed for the order counter
  (treat as budget exhausted) but log loudly; fail open for high-water mark
  (re-seed from current equity) since failing closed would permanently block.
- **Session crash mid-trade**: orders are placed one at a time and journaled
  immediately after placement; the next session reconciles journal vs
  `list_open_orders`/`get_positions` as its first step (identity .md instructs
  this).

## Testing

- `rails.py` and `performance.py` are pure → straight pytest units (position-cap
  math, order-budget day rollover, circuit-breaker edges, Sharpe/benchmark math
  against hand-computed fixtures).
- `broker.py` protocol gets a `FakeBroker` used to test `server.py` tool
  behavior (rails wired to tools, error strings) without network.
- One integration-gated test module (`ALPACA_INTEGRATION_TEST=1`) that hits the
  real paper API: place a 1-share limit order far from market, verify, cancel.
- The official `alpacahq/alpaca-mcp-server` is used once, manually, as a
  credential/paper-flow validation spike; it is not a dependency.

## Rollout

1. Merge this branch → deploy (`docker compose up -d --build`).
2. the user creates the free Alpaca paper account, puts paper keys in `.envrc`.
3. Debug-CLI smoke test (`claub-logs` skill) — verify tools list, place+cancel a
   paper order via conversation.
4. Say hello in the Discord channel; create the two schedules via the agent.
5. Let it run for several weeks; judge by the weekly scoreboards.
6. Paper→live is a deliberate, separate decision: new keys, `ALPACA_PAPER=false`,
   `ALPACA_LIVE_CONFIRMED=I_UNDERSTAND`, and a review of rail limits first.

## Open items

- Alpaca paper API keys (the user; blocks step 3 of rollout, not implementation).
- Avatar image for the trader agent (cosmetic, whenever).
