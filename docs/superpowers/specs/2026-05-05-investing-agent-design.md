# Investing Agent — Design

## Goal

Add a Claub agent (`investor`) that does market research and places trades, plus the MCP server it needs (`alpaca`). Paper trading from day one; real-money execution is the same code path, gated behind a single env-var flip.

The agent itself is a generic shell — identity + memory pattern, like `journalist`. The actual investing strategy, watchlists, and rhythm are intentionally NOT baked into the agent prompt; they live in the agent's workspace `CLAUDE.md` and evolve through user/agent collaboration.

## Non-goals

- No backtesting tools exposed via MCP (slow, multi-step, awkward through tool-call boundary). User can run lumibot backtests directly as workspace scripts if they want to.
- No agent-side risk caps in the MCP. Paper-by-default is the only safety net the MCP provides; behavior shaping is a workspace-config concern.
- No options-data alternative provider (Polygon.io, etc.). Alpaca's free options data is good enough for v1; Polygon goes in as a separate MCP later if needed.
- No opinionated investing style in the agent prompt (no "value investor" / "swing trader" framing). The agent is a generic personal analyst.

## Library choice

**`lumibot`** as the broker abstraction layer (1.5k stars, last release 2026-05-01, last push 2026-05-05, not archived). Wraps Alpaca, Interactive Brokers, Tradier, ccxt, and others behind one Broker interface. The user explicitly wants this for a future graduation path off Alpaca to a heavier broker (likely IBKR).

**`alpaca-py`** (official Alpaca SDK; 1.3k stars, last release 2026-04-29) for the ~25% of capabilities lumibot doesn't standardize: news, options chains, Alpaca-specific corporate actions. These tools are broker-coupled and would need re-wiring at graduation time.

We use lumibot's `Broker` abstraction directly, not its `Strategy` framework. The `Strategy` class is lumibot's headline feature for live algo deployment, but for an MCP exposing individual tools to an LLM agent the lower-level `Broker` API is the right surface.

## Components

### 1. `mcps/alpaca/` — new MCP server

Lives at `~/docker/claub/mcps/alpaca/` (instance dir, not the repo). Auto-`uv sync`'d on container start by the existing entrypoint convention. FastMCP, subprocess stdio (same shape as the existing `nextcloud` and `google` MCPs).

#### Tool surface

Tools are typed `@mcp.tool()` wrappers, each returning a trimmed dict, not raw API responses (matching the `hass` MCP convention from project `CLAUDE.md`).

**Account / portfolio (lumibot Broker):**
- `get_account()` → cash, equity, buying_power, paper-or-live indicator
- `get_positions()` → list of {symbol, qty, avg_entry, market_value, unrealized_pnl}
- `get_orders(status="all"|"open"|"closed", limit=50)`
- `get_portfolio_history(period="1D"|"1W"|"1M"|"3M"|"1Y")` → equity curve points

**Market data (lumibot Broker):**
- `get_quote(symbol)` → last price, bid, ask, timestamp
- `get_bars(symbol, timeframe="1Min"|"5Min"|"1H"|"1D", lookback)` → OHLCV
- `get_market_clock()` → is_open, next_open, next_close

**Trading (lumibot Broker):**
- `place_order(symbol, qty, side, type="market"|"limit"|"stop"|"stop_limit", limit_price?, stop_price?, time_in_force="day"|"gtc")`
- `cancel_order(order_id)`
- `get_order(order_id)`

**Research (alpaca-py direct, broker-coupled):**
- `get_news(symbols?, limit=20, since?)` → headlines + summaries from Alpaca's news feed
- `get_options_chain(symbol, expiry?)` → calls/puts with strikes, IV, greeks
- `get_corporate_actions(symbol, lookback_days=30)` → splits, dividends, mergers

#### Configuration

Env vars passed by the agent's `.mcp.json`:
- `ALPACA_API_KEY` — required
- `ALPACA_API_SECRET` — required
- `ALPACA_PAPER` — `true` / `false`. **Defaults to `true` if unset.** This is the single switch determining paper vs live.

Both keys live in the instance `.env` (already gitignored). The MCP refuses to start if the keys are missing.

#### Error handling

- **Transient Alpaca outages** (5xx, timeout, connection reset): retry 3× with exponential backoff (200ms, 800ms, 3.2s). After exhaustion, raise a structured error the agent can read.
- **Auth failure** (401/403): no retry. Raise immediately with a clear "credentials need refresh" message. Agent can surface to Discord.
- **Live-trading guardrail**: if `ALPACA_PAPER=false`, the MCP logs `WARNING: LIVE TRADING ENABLED — placing real-money order: {symbol} {qty} {side}` on every order placement to stderr (visible in `docker compose logs`). No silent surprises.
- **Order rejection** (insufficient buying power, market closed, bad symbol): pass through Alpaca's error message verbatim — these are agent-actionable.

### 2. `config/agents/investor.md` — agent identity

Generic, journalist-shaped. Contains:
- Identity: "personal investing analyst" (deliberately *not* "trader" — sets a thoughtful tone)
- Brief philosophy: think before trading; admit uncertainty; don't churn; if there's nothing to do, do nothing
- Reference to workspace `CLAUDE.md` for *what* to focus on (watchlists, theses, criteria)
- "How to work" — research before suggesting; never place a trade without explaining the thesis in the same Discord message
- Output style — Discord-friendly, terse, source links wrapped in `<>` to suppress embeds (matching journalist style)
- Memory section — see "Memory structure" below

The agent prompt does NOT contain:
- Specific tickers or watchlists
- Specific investing styles or strategies
- Schedule instructions (the agent will manage its own schedules via the existing `mcp__schedules__*` tools if it wants to)
- Risk caps or position sizing rules

Those are all workspace-config or agent-self-managed concerns.

### 3. `config/agents/investor.mcp.json` — agent MCP wiring

```json
{
  "mcpServers": {
    "playwright": {
      "type": "http",
      "url": "http://host.docker.internal:3854/mcp"
    },
    "alpaca": {
      "command": "uv",
      "args": ["--directory", "/claub/mcps/alpaca", "run", "server.py"],
      "env": {
        "ALPACA_API_KEY": "${ALPACA_API_KEY}",
        "ALPACA_API_SECRET": "${ALPACA_API_SECRET}",
        "ALPACA_PAPER": "${ALPACA_PAPER:-true}"
      }
    }
  }
}
```

Playwright port allocated per the existing per-agent convention (next free port after the highest currently used; will verify at implementation time). The investor agent gets its own Playwright profile so it can log in to financial sites if needed (yahoo finance, SEC EDGAR pro features, broker dashboards for visual checks).

### 4. `workspaces/investor/CLAUDE.md` — fluid config

Starts as a near-empty stub with:
- Reminder that we're in paper-trading mode
- Empty watchlist section
- A "fill in your goals here" prompt block
- Note that the agent can self-modify this file when the user asks to shift focus

### 5. `config/agents.yaml` entry

```yaml
agents:
  investor:
    channel_id: "..."   # user provides at deploy time
    display_name: "Investor"
```

No schedule entry. No `on_start`/`on_stop` hooks beyond the global Playwright bridge ones (which already cover any agent via `$CLAUB_AGENT_NAME`).

### 6. `config/settings.json` — allowlist

Add `mcp__alpaca__*` to `permissions.allow` (matching the existing per-MCP wildcard pattern: `mcp__playwright__*`, `mcp__schedules__*`, `mcp__google_*__*`).

### 7. Memory structure

Per the project's memory protocol, the agent gets a memory dir at `/claub/workspaces/investor/memory/`. Recommended structure documented in the agent.md:

```
memory/
  index.md              # active theses + open positions snapshot
  theses/
    AAPL.md             # one file per researched ticker — why we like/dislike, target, last reviewed
    NVDA.md
  trades/
    2026-05-05.md       # daily journal of what was placed and why
```

Agent reads `index.md` on every session start (per global memory protocol). Pruning rules:
- Trade journals: keep last 30 days. Older trade journals are deleted outright — anything load-bearing for an open position's reasoning should already live in `theses/{ticker}.md`, which is the durable record.
- Theses: keep while position is open OR ticker is on watchlist. When neither has been true for 90 days, delete.

## Data flow (one trade)

```
Discord msg → router → AgentProcess(investor)
  → agent reads memory/index.md
  → agent calls mcp__alpaca__get_quote, get_news, etc.
  → agent decides, calls mcp__alpaca__place_order(symbol, qty, type, side)
  → MCP retries on transient failure, logs LIVE warning if non-paper
  → lumibot Broker.submit_order() → Alpaca paper API
  → confirmation returns through chain
  → agent writes thesis to memory/theses/{ticker}.md
  → agent posts Discord summary including thesis + order details
```

## Testing

- **Unit tests** for each tool wrapper using mocked broker responses (no live API calls). Lumibot ships test fixtures we can lean on.
- **Smoke-test script** at `mcps/alpaca/smoke_test.py` that hits the real Alpaca *paper* API with a tiny set of round-trips: get_account, get_quote on SPY, place + cancel a limit order well outside the spread, get_positions. Runnable via `docker exec`. Confirms credentials + tool wiring end-to-end.
- **No integration tests against live trading.** Ever.

## Deployment

Standard Claub deploy flow:
1. User mints Alpaca paper-account API keys at alpaca.markets, adds to `~/docker/claub/.env`
2. Drop the `mcps/alpaca/` dir into `~/docker/claub/mcps/`
3. Drop `investor.md`, `investor.mcp.json` into `~/docker/claub/config/agents/`
4. Add agents.yaml entry + settings.json allowlist line
5. Create the Discord channel, paste its ID into agents.yaml
6. `docker compose restart` (entrypoint auto-runs `uv sync` on the new MCP)
7. First Discord message to the channel triggers lazy agent startup

Going live (later): edit `.env`, set `ALPACA_PAPER=false`, `docker compose restart`. The MCP will start logging `WARNING: LIVE TRADING ENABLED` lines on every order placement.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Alpaca outage during a planned trade | MCP retries 3× with backoff; agent surfaces failures to Discord rather than silently dropping |
| Agent makes a weird trade in paper, would be costly in live | Default `ALPACA_PAPER=true`; live flip is explicit and warned per-order in logs |
| Lumibot's Broker layer has rough edges (less tested than Strategy) | Smoke test catches the common cases; we accept some implementation friction as the cost of broker-portability |
| Future IBKR migration breaks news/options-chain tools | Acknowledged — those ~25% of tools are broker-coupled by design. Migration writeup will note them. |
| Agent self-edits workspace `CLAUDE.md` in destructive ways | Same risk as every other Claub agent; relies on the global memory protocol's "current state wins" + git history of the workspace |

## Out of scope (future work)

- Polygon.io integration for richer options / breadth data
- Backtesting workflow (would be a workspace script + agent runbook, not a new MCP)
- Migration to IBKR — separate spec when the user is ready
- Tax-lot tracking / harvest tools
- Multi-account support (e.g., separate paper and live accounts visible to the agent simultaneously)
