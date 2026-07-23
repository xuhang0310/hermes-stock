# Vibe-Trading Swarm Preset Inventory

## Scope

Source inspected:

- `E:/workspace/Vibe-Trading/agent/src/swarm/presets`
- `E:/workspace/Vibe-Trading/agent/src/swarm/runtime.py`
- `E:/workspace/Vibe-Trading/agent/src/swarm/task_store.py`

Inventory date: 2026-07-23.

## Summary

Vibe-Trading currently contains 30 YAML Swarm presets. All 30 parse as
valid directed acyclic graphs.

| Metric | Value |
| --- | ---: |
| Presets | 30 |
| Agent specifications | 118 |
| Task nodes | 118 |
| Dependency edges | 88 |
| Average agents per preset | 3.93 |
| Average DAG layers | 2.33 |

Agent count distribution:

| Agents | Presets |
| ---: | ---: |
| 3 | 5 |
| 4 | 23 |
| 5 | 1 |
| 6 | 1 |

Layer distribution:

| Layers | Presets |
| ---: | ---: |
| 2 | 21 |
| 3 | 8 |
| 4 | 1 |

The largest team is `technical_analysis_panel` with six agents and a
maximum parallel width of five. The deepest workflow is
`equity_research_team` with four serial layers.

## Structural Archetypes

The 30 presets reduce to four reusable DAG shapes:

1. Parallel research followed by one synthesizer: 21 presets.
2. Parallel research, synthesis, then review or decision: 5 presets.
3. Three-node serial chain: 3 presets.
4. Four-node serial chain: 1 preset.

This means Hermes does not need 30 different orchestrators. It needs one
durable DAG runtime plus versioned template data.

## Preset Inventory

### Equity and company research

| Preset | Shape | Purpose |
| --- | --- | --- |
| `equity_research_team` | 4-layer serial | Macro, sector, stock, report |
| `fundamental_research_team` | 3 parallel + synthesis | Financials, valuation, quality |
| `earnings_research_desk` | 3 parallel + synthesis | Fundamentals, revisions, event options |
| `value_investing_committee` | 4 parallel + chair | Adversarial value-investing review |

### Quantitative research and strategy

| Preset | Shape | Purpose |
| --- | --- | --- |
| `quant_strategy_desk` | 2 parallel, backtest, risk | Screening and factor-driven strategy |
| `factor_research_committee` | 2 parallel, combine, review | Factor mining and validation |
| `ml_quant_lab` | 2 parallel + backtest | Features and model validation |
| `pairs_research_lab` | 2 parallel, strategy, review | Correlation and cointegration |
| `statistical_arbitrage_desk` | 2 parallel, strategy, risk | Statistical arbitrage research |
| `technical_analysis_panel` | 5 parallel + synthesis | Multi-method technical consensus |
| `sector_rotation_team` | 3 parallel + synthesis | Cycle, prosperity, and flow rotation |
| `event_driven_task_force` | 3-layer serial | Event scan, impact, strategy |

### Portfolio, committee, and risk

| Preset | Shape | Purpose |
| --- | --- | --- |
| `investment_committee` | Bull/bear, risk, PM | Adversarial investment decision |
| `risk_committee` | 3 parallel + sign-off | Drawdown, tail risk, regime |
| `portfolio_review_board` | 3 parallel + CIO | Attribution, risk, execution |
| `etf_allocation_desk` | 3 parallel + optimizer | ETF portfolio construction |
| `global_allocation_committee` | 3 markets + allocator | Cross-market allocation |
| `fund_selection_panel` | 3-layer serial | Fund screen, attribution, FOF |

### Macro and cross-market

| Preset | Shape | Purpose |
| --- | --- | --- |
| `macro_strategy_forum` | 3 parallel + synthesis | Global, domestic, policy |
| `macro_rates_fx_desk` | 3 parallel + PM | Rates, FX, commodities |
| `geopolitical_war_room` | 3 parallel + synthesis | Geopolitics and supply shocks |
| `global_equities_desk` | 3 markets + synthesis | A-share, HK/US, crypto |

### Asset-class specialists

| Preset | Shape | Purpose |
| --- | --- | --- |
| `commodity_research_team` | 2 parallel + synthesis | Supply and demand cycle |
| `convertible_bond_team` | 3 parallel + synthesis | Bond floor, equity, option value |
| `credit_research_team` | 3 parallel + synthesis | Credit, rates, sector |
| `derivatives_strategy_desk` | 3-layer serial | Volatility, strategy, Greeks |
| `crypto_research_lab` | 3 parallel + synthesis | On-chain, DeFi, sentiment |
| `crypto_trading_desk` | 3 parallel + risk | Funding, liquidation, flow |

### Sentiment and alternative data

| Preset | Shape | Purpose |
| --- | --- | --- |
| `sentiment_intelligence_team` | 3 parallel + synthesis | News, social, capital flow |
| `social_alpha_team` | 3 parallel + synthesis | Twitter, Telegram, Reddit |

## Recommended Hermes Adoption

### Validation templates

Implement these first because together they exercise every important runtime
shape:

1. `fundamental_research_team`: basic fan-out and fan-in.
2. `quant_strategy_desk`: parallel nodes followed by two dependent stages.
3. `equity_research_team`: deepest serial workflow.
4. `technical_analysis_panel`: maximum parallel-width and aggregation test.

### First production wave

After the runtime is stable, prioritize the A-share workflows that match
existing Hermes data and digital-human capabilities:

- `investment_committee`
- `risk_committee`
- `portfolio_review_board`
- `factor_research_committee`
- `sector_rotation_team`
- `event_driven_task_force`
- `earnings_research_desk`
- `sentiment_intelligence_team`

Do not initially migrate crypto, social-platform, FX, derivatives, credit, or
global broker workflows unless the corresponding data connectors and
permission boundaries exist.

## Runtime Decision

Reuse Vibe-Trading's declarative concepts:

- agents
- tasks
- `depends_on`
- `input_from`
- topological validation
- same-layer parallelism
- upstream failure propagation

Do not copy its execution substrate:

- background daemon threads
- `ThreadPoolExecutor` as the durable scheduler
- per-run JSON files as the source of truth
- process-local cancellation

Hermes should execute every DAG node as an existing durable `AgentRun`, use
MySQL as the source of truth, Redis for dispatch and wakeup, and AgentScope as
the node execution engine.
