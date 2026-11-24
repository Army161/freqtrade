# AI Trading Command Center — Research Brief

_Compiled 2026-11-24. Every factual claim links to a source. Star counts were read from GitHub repo pages on 2026-11-24 (the GitHub API was not reachable from this environment), so treat them as approximate. "Verified" means primary/auditable source; "reported" means secondary press or vendor claims._

---

## 1. Core engines: Freqtrade and Hummingbot

### Freqtrade
| Item | Finding |
|---|---|
| Latest release | **2026.10** (30 Oct 2026): order-book websocket support, bot loop decoupled from RPC messaging, per-instance strategy parameters, Python 3.11 dropped ([releases](https://github.com/freqtrade/freqtrade/releases)). 2026.9 added an exchange clock-offset check, open-interest informative pairs and liquidation warnings. 2026.8 removed Bitmart because the exchange shut down ([releases](https://github.com/freqtrade/freqtrade/releases)). Release cadence is monthly. |
| Repo | ~57.2k stars, Python 3.12+. Spot: Binance, Bybit, Gate, HTX, Hyperliquid, Kraken, OKX and others. Futures: Binance, Bitget, Bybit, Gate, Hyperliquid, Kraken, OKX ([repo](https://github.com/freqtrade/freqtrade)). |
| FreqAI | Trains ML models (LightGBM, XGBoost, CatBoost, CNN; PyTorch and RL in separate images) on your own features and labels, and retrains on a background thread in dry/live runs. Limits: the example strategy is "not designed to be run in production", it can't be used with dynamic VolumePairlists, and CatBoost has no ARM wheels ([docs](https://www.freqtrade.io/en/stable/freqai/)). |
| Fleet features | **Producer/consumer mode**: one producer, for example a FreqAI box, streams its analyzed dataframes over websocket to many lightweight consumers ([docs](https://www.freqtrade.io/en/stable/producer-consumer/)). FreqUI can show an overview of several connected bots and switch between them, but each bot has its own login and CORS has to be configured ([FreqUI docs](https://gitcode.com/GitHub_Trending/fr/freqtrade/blob/develop/docs/freq-ui.md), [FTUI write-up](https://www.prompts.brightcoding.dev/blog/stop-wrestling-with-browser-tabs-monitor-freqtrade-bots-in-your-terminal-with-ftui)). In practice people run one Docker Compose service per bot, each with its own `user_data`, DB and API port. The official quickstart covers only a single instance ([docker docs](https://www.freqtrade.io/en/stable/docker_quickstart/)). |
| Monitoring | Community Prometheus exporters exist, such as ftmetric ([gist](https://gist.github.com/kamontat/acd2a7b9b391c725c9fc06b70ff6fe0a)) and thraizz/freqtrade-dashboard for comparing multiple instances ([repo](https://github.com/thraizz/freqtrade-dashboard)), plus Grafana dashboard 14632 ([grafana](https://grafana.com/grafana/dashboards/14632)). None of them is official. |
| MCP | kukapay/freqtrade-mcp (MIT) wraps the REST API ([PyPI](https://pypi.org/project/freqtrade-mcp/)). |

**NostalgiaForInfinity (NFI)**: ~3.5k stars, GPL-3.0, roughly 28.9k commits. The README requires a 5m timeframe, 6–12 open trades, a 40–80 pair volume pairlist, stablecoin quotes and blacklisted leveraged tokens ([repo](https://github.com/iterativv/NostalgiaForInfinity)). **I found no audited or public live-performance record.** The repo says backtest results are posted in commit comments ([repo](https://github.com/iterativv/NostalgiaForInfinity)), and searches for public live or dry-run tracking turned up nothing ([search notes](https://awesome.ecosyste.ms/projects/github.com%2Fiterativv%2FNostalgiaForInfinity)). Treat any NFI profit claim as unverified.

### Hummingbot
| Item | Finding |
|---|---|
| Latest release | **v2.18.0 (20 Nov 2026)**, after 2.17.0 (22 Sep) and 2.16.0 (29 Jul) ([release index](https://hummingbot.org/release-notes/)). 2.18 adds idempotent order placement and `GET /system/info` to the API. Condor gains an Agent Hub, risk-gated delegated runs, and an emergency shutdown that "actually stops bots" ([2.18.0 notes](https://hummingbot.org/release-notes/2.18.0/)). |
| Repo | ~21.4k stars, Apache-2.0, Python. Deploys from source (conda, `make install`) or with Docker Compose (`make setup && make deploy`) ([repo](https://github.com/hummingbot/hummingbot)). |
| V2 framework | **Executors** (Position, DCA, Grid, TWAP, XEMM, LP) are self-contained order workflows. **Scripts** are for prototyping. **Controllers** are production sub-strategies, and several can be loaded into one bot via `v2_with_controllers.py`. A **Market Data Provider** supplies candles, order books and trades ([docs](https://hummingbot.org/strategies/v2-strategies/)). |
| hummingbot-api | A FastAPI hub that deploys, starts, stops and archives bot containers. It uses PostgreSQL for orders, trades and positions, and an EMQX broker for bot command and telemetry. Each bot gets an isolated directory. The docs advise against exposing port 8000 publicly and recommend Tailscale instead ([docs](https://hummingbot.org/hummingbot-api/)). API, MCP, Skills, Quants-Lab and Condor are continuously deployed from `main` with no fixed versions ([2.16.0 notes](https://hummingbot.org/release-notes/2.16.0/)). |
| Condor | An open-source "harness for trading agents". It splits LLM reasoning (an OODA loop) from deterministic execution and risk enforcement, which runs through hummingbot-api. Interfaces are Telegram, a web dashboard (localhost:8088) and a CLI ([docs](https://hummingbot.org/condor/)). The model picker supports OpenRouter, Ollama and LM Studio ([2.16.0](https://hummingbot.org/release-notes/2.16.0/)). |
| Exchanges | 39 connectors listed, including Binance, OKX, Kraken, Coinbase and Hyperliquid, plus Gateway DEXs such as Uniswap, Raydium and Jupiter ([exchanges](https://hummingbot.org/exchanges/)). |
| MCP | Official hummingbot/mcp. It needs a running hummingbot-api ([repo](https://github.com/hummingbot/mcp)). |

**Takeaway:** Hummingbot already ships the fleet-orchestration layer we would otherwise build (hummingbot-api plus Condor). Freqtrade does not, so its fleets are hand-rolled Compose stacks plus producer/consumer.

---

## 2. Other open-source engines and agents (candidate cluster members)

| Project | Stars | License | Lang | Good at | Notes |
|---|---|---|---|---|---|
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | ~31.8k | LGPL-3.0 | Rust core, Python | Deterministic event-driven backtest/live parity, low latency | Adapters for Binance, Bybit, Coinbase, Deribit, Kraken, OKX, dYdX, Hyperliquid, Polymarket. v2 is at RC stage ([repo](https://github.com/nautechsystems/nautilus_trader)) |
| [Jesse](https://github.com/jesse-ai/jesse) | ~8.8k | MIT | Python | Clean research→live workflow | Supports live and paper trading. The repo page doesn't say whether live trading is free ([repo](https://github.com/jesse-ai/jesse)) |
| [OctoBot](https://github.com/Drakkar-Software/OctoBot) | ~7.0k | GPL-3.0+ | Python | Grid/DCA, TradingView alerts, OpenAI/Ollama connectors | 15+ exchanges incl. Hyperliquid ([repo](https://github.com/Drakkar-Software/OctoBot)) |
| [Superalgos](https://github.com/Superalgos/Superalgos) | ~5.8k | Apache-2.0 | JS | Visual strategy design | Activity level unclear from the repo page ([repo](https://github.com/Superalgos/Superalgos)) |
| [Lumibot](https://github.com/Lumiwealth/lumibot) | ~2.3k | GPL-3.0 | Python | Multi-asset (stocks/options/crypto) across 12 brokers | AI-agent features ([repo](https://github.com/Lumiwealth/lumibot)) |
| [vectorbt](https://github.com/polakowo/vectorbt) | ~9.9k | Apache-2.0 + **Commons Clause** | Python/Numba | Fast vectorized parameter sweeps | You may not sell a product that is primarily vectorbt. Advanced features are in the paid PRO edition ([repo](https://github.com/polakowo/vectorbt)) |
| [Backtrader](https://github.com/mementum/backtrader) | ~23.8k | GPL-3.0 | Python | Classic event-driven backtests | Effectively in maintenance mode (old CI, forum-only support) ([repo](https://github.com/mementum/backtrader)) |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | ~123.6k | Apache-2.0 | Python | LangGraph multi-agent "trading firm": analysts, bull/bear debate, risk, PM | v0.7.1 (Oct 2026). **Research-only**: simulated exchange, no live broker ([repo](https://github.com/TauricResearch/TradingAgents)) |
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | ~64.8k | MIT | Python | Educational LLM "investor persona" agents | "The system does not actually make any trades" ([repo](https://github.com/virattt/ai-hedge-fund)) |
| [NOFX](https://github.com/NoFxAiOS/nofx) | ~13.3k | **AGPL-3.0** | Go + web | LLM perp trading: Binance, Hyperliquid, OKX, Bitget, Bybit, Aster, Lighter | Has an approval-gated "Frontier" mode and an autonomous "Autopilot" mode. Pushes a paid model gateway (Claw402) ([repo](https://github.com/NoFxAiOS/nofx)) |

**Hyperliquid / "Grok" agents:** I found **no official xAI trading agent**. xAI's open-sourced "Grok Build" is a coding tool ([alternativeto](https://alternativeto.net/news/2026/7/xai-has-open-sourced-grok-build-after-backlash-over-secretly-uploading-users-repositories/)). The Grok and Hyperliquid repos that do exist are small community projects, such as galleonlabs "hypergrok" ([listing](https://skillsllm.com/skill/hypergrok-trading-desk)), zoan37/hyperclaw ([repo](https://github.com/zoan37/hyperclaw)) and sanketagarwal/hyperliquid-trading-agent ([listing](https://www.sourcepulse.org/projects/28094581)). All are unaudited, and you should vet the code before giving any of them an API wallet.

**"JEV agent":** The best match is **Jev, a closed "System One" decision model from TypeSafe AI**. It takes structured state and returns typed choices or probabilities. It is a paid usage-based API, not open source. Some open-source wrappers exist, including a Hyperliquid "Jev Trade" repo ([OneBullEx analysis](https://www.onebullex.com/explore/can-jev-trade-crypto-live-bot-risk-checks)). Evidence of performance is poor. One reported simulated test lost $3,150 over 731 trades, about half of it to fees, and the public Jev Trade dashboard was offline when checked ([same](https://www.onebullex.com/explore/can-jev-trade-crypto-live-bot-risk-checks)). A "$10,000 to Jev" showcase explicitly "does not show a result" ([echai](https://echai.ventures/jev?category=Trading)). A JEV meme-token ecosystem also exists ([TechFlow](https://www.techflowpost.com/en-US/article/34173)). Several of these pages look like affiliate or SEO content. Integrating Jev would mean an external decision-model plugin, not a cluster engine.

---

## 3. Evidence from live LLM trading competitions

| Event | Setup | Outcome |
|---|---|---|
| **Alpha Arena S1** (Nof1, to 3 Nov 2025) | 6 frontier LLMs, $10k each of real money, crypto perps on Hyperliquid | Only **Qwen3-Max** and **DeepSeek V3.1** were profitable. Claude Sonnet 4.5, Grok 4, Gemini 2.5 Pro and GPT-5 lost money, and GPT-5 lost about 60%+ ([ForkLog](https://forklog.com/en/four-out-of-six-ai-models-suffer-losses-in-trading-tournament/), [Benzinga](https://fr.benzinga.com/quote/cryptocurrency/25/11/48651974/chatgpt-lost-63-trying-to-trade-crypto-but-one-china-ai-made-a-healthy-profit)). Qwen's reported return ranges from about 5% to about 22% depending on source ([iweaver](https://iweaver.ai/blog/alpha-arena-ai-trading-season-1-results), [datawallet](https://www.datawallet.com/crypto/alpha-arena-nof1-ai-explained)) |
| **Alpha Arena S1.5** (Nov–Dec 2025) | 8 models, US-stock tokens on trade.xyz, 4 prompt regimes | The anonymous "Mystery Model" (**Grok 4.20**) averaged about +12% and was the only model still profitable as of 8 Dec ([ForkLog](https://forklog.com/en/ai-model-grok-4-2-triumphs-in-trading-tournament/), [GNcrypto](https://www.gncrypto.news/news/mystery-model-alpha-arena-season-1-5-winner/)) |

Nof1 itself cautioned that early results "may be the result of luck", citing small samples and a short window ([ForkLog](https://forklog.com/en/four-out-of-six-ai-models-suffer-losses-in-trading-tournament/)). An independent analysis found that a random-trading baseline with similar leverage and frequency matched the best model about 10% of the time, which is not statistically significant. Its conclusion was that there is "very little supporting evidence that LLMs can trade skillfully" ([Magnus Ross](https://magnusross.github.io/posts/nof1-analysis)). I found no published Season 2 results.

**Implication:** Winners changed from season to season, and most participants lost, so this is noise rather than evidence of skill. LLMs are better suited to orchestration, regime labeling, summarization and human-in-the-loop proposals than to unsupervised order placement.

---

## 4. Reality check: "$20 → thousands in days"

- **Regulators:** The CFTC advisory "AI Won't Turn Trading Bots into Money Machines" says AI can't predict sudden market changes. It calls high or guaranteed returns red flags and cites a fraud that sold $100 entries into a bot promising 10% a month ([CFTC](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/aitradingbots.html)). The joint SEC/NASAA/FINRA alert warns about frauds that falsely claim to use AI ([Investor.gov](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/artificial-intelligence-fraud)). The SEC charged entities over a reported $15M+ fake "AI trading bot" scheme in which the displayed profits were fictitious ([OECD AI incident](https://oecd.ai/en/incidents/2026-09-29-c7ae)). The FCA is running international takedowns of illegal finfluencers ([FCA](https://www.fca.org.uk/news/press-releases/fca-leads-international-crackdown-illegal-finfluencers)).
- **Academic and official data:** Among Brazilian day traders who persisted for more than 300 days, **97% lost money**, and only 1.1% earned more than the minimum wage ([Chague et al., SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101)). A BIS study found that a **majority of crypto-app users in nearly all economies lost money on bitcoin** from 2015 to 2022 ([BIS Bulletin 69](https://www.bis.org/publ/bisbull69.htm)).
- **Arithmetic:** Turning $20 into $2,000 in 7 days is 100x, which needs about 93% compounded per day. Results like that come from survivorship-selected, leveraged lottery outcomes, not from a repeatable process.

**Small-account frictions:**

| Exchange | Spot fee (lowest tier) | Minimum order |
|---|---|---|
| Binance | 0.10% maker / 0.10% taker, 0.075% with BNB ([schedule](https://www.binance.com/en/fee/schedule)) | A per-pair NOTIONAL/MIN_NOTIONAL filter. 5 USDT is the common example, so check `exchangeInfo` ([API docs](https://developers.binance.com/docs/binance-spot-api-docs/filters), [Academy](https://www.binance.com/lv/academy/articles/binance-spot-trading-rules-a-comprehensive-guide)) |
| Kraken Pro | **0.40% maker / 0.80% taker** at $0+ volume, 0.30/0.60 from $2.5k ([Pro fees](https://www.kraken.com/pro/fees)). Instant buy is 1% plus spread ([fees](https://www.kraken.com/features/fee-schedule)) | 0.0001 BTC; 5 USD/EUR/GBP for fiat-base pairs ([support](https://support.kraken.com/articles/205893708-minimum-order-size-volume-for-trading)) |
| OKX | Depends on tier, set by OKB holdings. OKX's own FAQ example uses 0.08% maker / 0.10% taker; check the live schedule ([FAQ](https://www.okx.com/en-us/help/trading-fee-rules-faq)) | Per-instrument `minSz`; read it from the API |

With $20 and a $5 minimum you can hold at most 3–4 positions, while NFI wants 6–12. At Kraken's taker rate, a round trip costs 1.6%, so a strategy needs that much edge per trade just to break even.

---

## 5. Validation and risk-management best practice

- **Walk-forward analysis** (Pardo): optimize on an in-sample window, test on the next out-of-sample window, roll forward, and judge only the combined OOS results. Anchored and rolling variants exist. Use purge gaps between windows to prevent leakage, and don't choose the variant after seeing which looks better ([Wikipedia](https://en.wikipedia.org/wiki/Walk_forward_optimization), [arXiv 2603.09219](https://arxiv.org/html/2603.09219)). OOS testing still suffers from selection bias if you iterate on it ([Harbourfront](https://harbourfrontquant.substack.com/p/the-limits-of-out-of-sample-testing)).
- **Multiple testing:** The best Sharpe ratio out of N trials is inflated, and the inflation grows with N. The **Deflated Sharpe Ratio** corrects for the number of trials and for non-normal returns ([Bailey & López de Prado, SSRN 2460551](https://papers.ssrn.com/abstract=2460551)). The **Probability of Backtest Overfitting**, estimated with CSCV, quantifies how often the in-sample winner underperforms out of sample ([Bailey, Borwein, López de Prado, Zhu, SSRN 2326253](https://papers.ssrn.com/abstract=2326253)). The **Minimum Backtest Length** concept is covered in [this paper](https://escholarship.org/content/qt95t7k79q/qt95t7k79q.pdf). The command center must log **every** trial (hyperopt epochs, strategy variants) so it can compute DSR and PBO.
- **Freqtrade-native checks:** `lookahead-analysis` re-runs signals one at a time to detect future leakage. It can miss signals that never fire ([docs](https://www.freqtrade.io/en/stable/lookahead-analysis/)). `recursive-analysis` is documented next to it.
- **Freqtrade Protections:** StoplossGuard, MaxDrawdown (`calculation_mode: "equity"` is recommended), LowProfitPairs and CooldownPeriod, all defined as a strategy property ([docs](https://www.freqtrade.io/en/stable/plugins/)).
- **Kill switches:** Use layered controls: a bot-level stop/stopentry, a fleet-level emergency stop (Condor 2.18 fixed its emergency shutdown, [notes](https://hummingbot.org/release-notes/2.18.0/)), and an exchange-side dead-man's switch. On Kraken, `CancelAllOrdersAfter` with a 60s timeout, refreshed every 15–30s, cancels all orders if the controller goes silent ([Kraken docs](https://docs.kraken.com/api/docs/rest-api/cancel-all-orders-after/)).
- **Position sizing:** Kelly maximizes long-run growth but can produce deep drawdowns, and estimation error makes full Kelly dangerous. Fractional Kelly trades growth for safety. With a 2% edge, half-Kelly keeps about 75% of the growth rate and has about an 89% chance of doubling before halving ([Thorp](https://www.Gwern.net/doc/statistics/decision/2006-thorp.pdf), [MacLean–Thorp–Ziemba](https://www.stat.berkeley.edu/~aldous/157/Papers/Good_Bad_Kelly.pdf)). Cap the fraction at ≤0.25–0.5 Kelly, using edge estimates from OOS data only.

---

## 6. Data and infrastructure stack (2026)

| Layer | Recommendation | Evidence |
|---|---|---|
| Market data | **ccxt** (REST) plus the built-in **ccxt pro** websockets (`watchOrderBook`, etc.), now free and in the same package | [ccxt docs](https://docs.ccxt.com/docs/pro) |
| Storage | Parquet/Feather for research candles (what Freqtrade uses natively); QuestDB or TimescaleDB for ticks and fleet telemetry. QuestDB's own Aug 2026 TSBS benchmark shows 8.59M vs 1.08M rows/s ingestion against TimescaleDB, but it is vendor-run and uses a fork of TSBS | [QuestDB](https://questdb.com/blog/timescaledb-vs-questdb-comparison/) |
| Orchestration | Docker Compose per host. Hummingbot bots via hummingbot-api, Freqtrade bots as Compose services. Move to K8s only once there are many hosts (community Helm charts exist, e.g. [DerSalvador/freqtrade-helm-chart](https://github.com/DerSalvador/freqtrade-helm-chart)) | [hummingbot-api](https://hummingbot.org/hummingbot-api/) |
| Private network | Tailscale; never expose bot APIs publicly | [hummingbot-api](https://hummingbot.org/hummingbot-api/), [2.15.1](https://hummingbot.org/release-notes/2.15.1/) |
| Monitoring | Prometheus + Grafana. Freqtrade via community exporters (ftmetric or freqtrade-dashboard); Hummingbot metrics via the API's Postgres | [ftmetric gist](https://gist.github.com/kamontat/acd2a7b9b391c725c9fc06b70ff6fe0a), [freqtrade-dashboard](https://github.com/thraizz/freqtrade-dashboard) |
| MCP servers | **Official ccxt MCP** (`npx -y ccxt-mcp`, with sandbox/trading switches); **hummingbot/mcp**; **kukapay/freqtrade-mcp** (community) | [ccxt MCP](https://docs.ccxt.com/docs/mcp), [hummingbot/mcp](https://github.com/hummingbot/mcp), [freqtrade-mcp](https://pypi.org/project/freqtrade-mcp/) |

---

## Implications for our design

1. **Use Hummingbot's stack rather than rebuilding it.** Treat hummingbot-api as the Hummingbot adapter, and write a thin equivalent for Freqtrade that wraps each bot's REST API and manages Compose services. One control plane then drives both engines through a common `BotAdapter` interface (start, stop, stopentry, forceexit, status, PnL).
2. **Keep LLMs advisory by default.** Competition evidence shows no reliable LLM trading skill. LLM agents should propose and the deterministic engines should execute. Any autonomous mode should go through a hard risk gate with per-bot and fleet loss limits, as in Condor's design.
3. **Build in statistical honesty.** Log every backtest and hyperopt trial centrally, report DSR and PBO alongside Sharpe, require walk-forward OOS plus `lookahead-analysis` and `recursive-analysis` to pass, and require a dry-run period before any bot is promoted to live.
4. **Layer the kill switches.** Have per-bot Protections, fleet-wide circuit breakers (portfolio drawdown, API-error rate, clock drift), and exchange dead-man's switches where supported. Test the emergency stop end to end, since Condor only fixed its own in 2.18.
5. **Keep a small-account guardrail.** Before going live, check minimum notional and fee drag per exchange, warn when a strategy needs more slots than the balance allows, and never present hype-style return projections in the UI.
6. **Mind licenses.** Freqtrade and NFI are GPL-3.0, NOFX is AGPL-3.0 and vectorbt has a Commons Clause. Integrate them as separate processes over APIs, not by linking or vendoring, to keep the command center's own license flexible.
7. **Keep research-only agents out of the order path.** TradingAgents and ai-hedge-fund can serve as "analyst" signal sources feeding proposals, never as order routers. Jev and Grok community agents are optional external plugins, sandboxed in dry-run.
