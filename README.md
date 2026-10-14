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

## Status

Phase 0 (data spine) and Phase 1 (world state + mechanism engine) are under construction. See `docs/spec.md`
§13 for the backlog and `AGENTS.md` for conventions.
