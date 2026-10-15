# macrosys

A global-macro, cross-asset research and trading system. It takes an economic change, traces its direct and
indirect consequences through production networks, balance sheets, policy, and currencies, subtracts what
markets already price, and converts the residual into sized positions in liquid ETFs on a weekly cycle with
daily monitoring. The economic model is general; the first execution universe is narrow on purpose.

**Start with the specification:** [`docs/spec.md`](docs/spec.md). It defines the mechanism library, world
model, FX layer, risk monitoring, data contracts, evaluation, trade lifecycle, and the phased backlog that this
code implements. Section numbers in code comments and tickets (e.g. `§3.4`, `B03`, `A09`) refer to it.

## Layout

```
macrosys/
  core/          shared primitives: units vocabulary, Quantity, timestamps/horizons, contracts (protocols)
  store/         DuckDB/Parquet tables, raw payload store, point-in-time snapshot(as_of)
  sources/       DataSource connectors (FRED/ALFRED, ECB, CFTC, Treasury, SEC, Tiingo, MT5, TradingView...)
  io_tables/     input-output tables (OECD ICIO, BEA), Leontief algebra, import leakage, Domar weights
  mechanisms/    mechanism schema, loader, validator, composition rules, propagator
  instruments/   instrument master, corporate actions, total return, holdings -> exposure vectors (A09)
  risk/          aligned returns, factor model, shrinkage covariance, regimes, scenarios
  decide/        thesis object, gates, portfolio construction
  eval/          frozen forecast ledger, case library, scoring
  cli.py         entry point
mechanisms/      YAML mechanism records (the library itself; data, not code)
config/          source registry, mandate, universe (owner-edited data)
tests/           mirrors the package layout; no network access in tests
docs/            spec.md and decision records
```

## Quickstart

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,stats]"
.\.venv\Scripts\python -m pytest
```

Tests never touch the network; connectors are tested against stored fixtures under `tests/fixtures/`.

## Status (2026-10-14)

Built and tested (369 tests; 6 strict `xfail`s that name mechanism parameters still awaiting local estimation):

| Spec ticket | What exists | Package |
|---|---|---|
| D0.1 | DuckDB schema, raw payload store, point-in-time `snapshot(as_of)`, coverage gate, world state, run manifest | `store/` |
| D0.2, D0.3b | 20 connectors (FRED/ALFRED, BLS, BEA, Census, Treasury, Fed GSW, ECB, IMF, OECD, Comtrade, PortWatch, EIA, NY Fed, World Bank, Cboe, CFTC, SEC incl. N-PORT, Tiingo, MetaTrader 5, TradingView-API) with rate limits from `config/source_registry.yaml`, parser-change quarantine, fixture tests | `sources/`, `pipeline/` |
| D0.3 | Instrument master, holdings → exposure vectors (A09), total return from raw closes + actions, FX rule, aligned returns, cost | `instruments/` |
| W1.2 | OECD ICIO 2025 loader (50 industries), Leontief inverse, import leakage, Domar weights, cost shares | `io_tables/` |
| W1.3–W1.5 | Mechanism schema/loader/validator, composition rules §3.4, propagator with loops, counterfactual harness, **51 mechanism records** + 3 feedback loops | `mechanisms/`, `mechanisms/*.yaml` |
| W1.5–W1.6 | World-state recipes (point-in-time), 12 historical cases with cited structural outcomes, 9 capability tests | `world/`, `cases/`, `cases/*.yaml` |
| P2.1 | Curve bootstrap, implied policy path, breakeven decomposition, term premium, futures curve shape, reverse DCF, **priced-in resolver** | `pricing/` |
| P2.2, R3.3 | Thesis object (§7.7 in full), four gates with no-trade default, sizing, deterministic order tickets, order state machine, reconciler | `decide/` |
| P2.3 | LLM roles 1–4 with typed outputs, the three guardrails, anonymiser, TF-IDF retrieval, $25/month budget guard | `llm/` |
| R3.1–R3.2 | Ledoit–Wolf, EWMA, factor model with block-bootstrap intervals, absorption ratio, two-state variance regime, DCC, Forbes–Rigobon, scenarios | `risk/` |
| E4.1–E4.2 | Hash-chained forecast ledger, Brier/PSR/deflated Sharpe, purged walk-forward splits, baselines | `eval/` |
| X5.1 | SQLite OMS, paper broker, class-A halt on reconciliation break, kill switch, runbook hash check | `exec/`, `docs/runbook.md` |
| S0.1–S0.2 | Walking skeleton: one command runs shock → propagation → priced-in → gates → no-trade → ledger → manifest → report, on three unrelated shocks | `skeleton/`, `docs/walkthrough.md` |
| E4.4 | Release event study: ALFRED first prints → point-in-time surprises (AR(1) forecast, robust scale) → market responses at 1/5/20 days → predicted-sign tests with binomial and walk-forward checks, regime split, HC1 slopes written to `parameters/event_response.yaml`. Runs on the live store | `events/`, `config/events.yaml` |
| P2.4 (partial) | Survey-consensus surprises: a locally held calendar extract (forecast/actual/previous; research tier, never fetched by this repo) is loaded into `calendar_print`, mapped to seven US releases with the dropped unit restated, matched by release date, and accepted only when the printed actual agrees with the store's as-reported change. `--forecaster consensus` runs the event study against it | `events/consensus.py`, `config/events.yaml` `consensus:` |
| W1.6 (scoring) | Case replay labels each scored outcome evidential (sign from the recorded shock on library parameters) or formula-direction (author-fed input or replay fixture); the research gate reads evidential outcomes only | `cases/`, `cases/fixtures/replay_parameters.yaml` |

Try it: `.\.venv\Scripts\python -m macrosys.cli skeleton demo --out .scratch\demo`.

Event study on the ingested store: `python -m macrosys.cli events build --db data\store\macrosys.duckdb` then
`python -m macrosys.cli events study --db data\store\macrosys.duckdb --write-parameters parameters\event_response.yaml`.
With a local calendar extract (spec §9.8, P2.4): `python -m macrosys.cli events consensus-load --db data\store\macrosys.duckdb --sqlite <path to economic_data.db>`,
then add `--forecaster consensus` to `build` and `study`.

`macrosys weekly --config config/pipeline.yaml` ingests, then records that UTC date's forward decision. It does not place an order. A second run the same day keeps the first thesis. To record a date without ingesting: `python -m macrosys.cli forward week --config config/pipeline.yaml --as-of YYYY-MM-DD --shocks config/open_shocks.yaml --mandate config/mandate.yaml --ledger data/forward/ledger.jsonl --week-log data/forward/weeks.jsonl --out data/forward/YYYY-MM-DD`.
`forward status` prints the dry-run clock. An empty shock file is a recorded no-trade. Paper stays off until the research sign bar in the mandate is met, and the 8-week gate is not claimed before eight finished weeks are on the log.

Local projections: `python -m macrosys.cli estimate local --db data/store/macrosys.duckdb`. A coefficient replaces `TODO_estimate` only when its interval excludes zero and its sign is stable across windows. Six coefficients are on the cards, including the utilization ceiling and the cost-capture share. The rest of the report, including the slopes that did not clear the bar, is in `parameters/local_elasticities.yaml`.

Still open, on purpose: structural `TODO_estimate` elasticities (the event study estimates market-response slopes, not IO elasticities; a mechanism with a null parameter refuses to run), a terms-clean consensus source that can be fetched (§9.8; Fair Economy's terms block a scheduled fetch), historical case files still score one mechanism at a time unless `compose_path: true`, and any live broker adapter. The live adapter waits on the paper gates in §10.3. See `docs/spec.md` §13 and §15.
