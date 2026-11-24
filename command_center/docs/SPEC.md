# Trading Command Center (TCC) — Plan, Spec & Architecture (v1)

## 1. Goal (and the honest version of it)

One always-on station that **researches, validates, deploys, supervises and risk-manages**
clusters of open-source trading bots (Freqtrade + Hummingbot), with AI agents steering the
research and tightening risk, and a dashboard / API to control everything.

What v1 deliberately does **not** promise: guaranteed profit, "thousands of profitable plays
per second", or turning $20 into thousands in days. See `RESEARCH.md` §4: regulators flag
exactly those claims as fraud patterns, and the compounding they require (~93%/day) doesn't
exist in liquid markets. The system is built to *find out honestly* whether an edge exists,
and to lose little while finding out.

## 2. Architecture

```
                         ┌───────────────────────── Dashboard (/) ─────────────────────────┐
                         │ KPIs · equity · bots · research funnel · leaderboard · agents  │
                         └───────────────▲────────────────────────────────────────────────┘
                                         │ REST (FastAPI, 127.0.0.1:8800, bearer token)
┌────────────────────────────────────────┴──────────────────────────────────────────────┐
│                               ORCHESTRATOR (asyncio)                                   │
│  risk loop 10s        research loop 4h               agent loop 60m                    │
│  ├ snapshot bots      ├ Stage 1 numba screener       ├ ResearchDirector → search priors │
│  ├ RiskEngine rules   │   genetic search, WF folds   ├ RiskOfficer → tighten-only       │
│  ├ supervisor restart ├ OOS + Deflated Sharpe gate   └ PortfolioManager → promote/retire│
│  └ equity history     ├ Stage 2 Freqtrade backtest                                      │
│                       └ Hummingbot V2 backtests      providers: Claude → Grok → rules   │
│                          SQLite (WAL): genomes · runs · bots · events · equity · notes  │
└───────┬───────────────────────────────────────────────┬────────────────────────────────┘
        │ subprocess + REST (per-bot port, basic auth)   │ subprocess (HB venv) + logs
┌───────▼──────────────────────────┐          ┌─────────▼──────────────────────────────┐
│ Freqtrade cluster                │          │ Hummingbot cluster                     │
│ ft-xxxx  TCCGenome (any genome)  │          │ hb-xxxx headless v2_with_controllers   │
│ dry-run / live, protections,     │          │ bollinger_v1 · macd_bb_v1 · pmm_mister │
│ own sqlite + log + API port      │          │ <exchange>_paper_trade / live          │
└───────┬──────────────────────────┘          └─────────┬──────────────────────────────┘
        └──────────────── exchanges via ccxt / HB connectors (OKX, Kraken, …) ───┘
```

## 3. Strategy lifecycle (the "self-improvement" loop)

| Stage | Engine | Gate | Typical throughput |
|---|---|---|---|
| 0 Generate | genome.py (5 families × filters × exits × TF) | — | random + crossover + mutation, biased by agents |
| 1 Screen | numba simulator, 4 walk-forward IS folds | robust fitness (median+worst-fold Sharpe, DD, consistency) | ~30–40 genomes/s on 3 cores (≈120k/h) |
| 2 OOS | same simulator on held-out last 25% | Deflated Sharpe ≥ 0.9 over *all* trials, OOS Sharpe ≥ 0.5, DD ≥ −20% | top-40 per cycle |
| 3 Confirm | real Freqtrade backtester (fees, order logic, protections) | profit > 0, Sharpe ≥ 0.5, DD ≥ −20% | ~6 s per genome |
| 4 Incubate | paper bot in Freqtrade cluster | PortfolioManager retires losers after ≥15 trades; RiskEngine retires at −10% | up to `max_bots` |
| 5 Live | same bot with keys | **human only** (`mode: live` + `TCC_LIVE=1` + keys) | — |

Hummingbot controllers follow the same idea: random search over controller grids, ranked by
mark-to-market return/Sharpe/DD (never win-rate), best candidates run as paper nodes.

Each cycle uses fresh data, and agents re-weight where the search looks. That is the
recursive part. Promotion is always gated by out-of-sample evidence.

## 4. Risk model (defence in depth)

1. Exchange-level: sub-account, no withdrawal permission, IP allow-list (operator).
2. Bot-level (inside Freqtrade): stoploss per genome, CooldownPeriod, StoplossGuard,
   MaxDrawdown(equity), LowProfitPairs; HB: max global/controller drawdown in quote.
3. Station-level RiskEngine: portfolio DD → **kill switch**; daily loss → halt entries
   until next UTC day; bot DD → retire; losing streak → pause; exposure cap; heartbeat →
   supervisor restart with exponential backoff.
4. Agent guardrail: agents may only **tighten** limits; loosening requires editing station.yaml.
5. Live gating: three independent switches; paper is the default everywhere.

## 5. AI agents

| Agent | Sees | May do | Bounds |
|---|---|---|---|
| ResearchDirector | per-family trials / pass-rates / fitness | set family sampling weights | each weight clamped to [0.2, 3.0] |
| RiskOfficer | risk state, limits, last-24h warnings | tighten limits | tighten-only, enforced by RiskEngine |
| PortfolioManager | validated leaderboard, bot snapshots | deploy confirmed genomes to paper, retire losers | `max_bots`, never during halt/kill, never live |

Providers are tried in order: Claude (`claude-opus-5-5` via the Anthropic SDK, with server-side
refusal fallback), Grok (`XAI_API_KEY`), then deterministic rules. Every proposal is logged in
`agent_notes` along with whether it was applied.

## 6. Interfaces

CLI: `tcc up | research | status | leaderboard | kill | reset-kill | plan | download`

REST: `GET /api/{overview,bots,leaderboard,runs,events,equity,agents}`,
`GET /api/bots/{name}/log`,
`POST /api/{kill,reset-kill,research/run,agents/run,deploy/{id}}`,
`POST /api/bots/{name}/{pause|resume|exit-all|retire|stop}`

## 7. Roadmap (v2+)

- Shared market-data bus (ccxt websockets → QuestDB/Parquet), so bots stop polling individually.
- Combinatorially-purged CV + Probability of Backtest Overfitting (PBO) report per cycle.
- Correlation-aware portfolio allocation across bots (HRP / risk parity) instead of equal wallets.
- More engines behind the same cluster interface: NautilusTrader (execution), OctoBot (grid/DCA),
  Jesse; hummingbot-api + EMQX for Hummingbot fleet control over MQTT.
- Prometheus exporter + Grafana; Telegram/Slack alerts on risk events.
- Exchange dead-man's switch (e.g. Kraken `CancelAllOrdersAfter`) refreshed by the risk loop.
- MCP server exposing read-only station state to external assistants.
