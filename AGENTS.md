# Agent conventions — macrosys

Read [`docs/spec.md`](docs/spec.md) first. It is the source of truth for what is being built and why. Section
references (`§3.4`), mechanism IDs (`A01`, `B03`, `C07`), and ticket IDs (`D0.2`, `W1.3`) all point into it.

## Non-negotiables

- **Units on every number.** Use `macrosys.core.units` and `macrosys.core.quantity.Quantity`. A function that
  returns a bare float for an economic quantity is a bug.
- **Three timestamps on every observation:** reference period, `published_at`, `ingested_at`. Point-in-time
  reads go through `snapshot(as_of)`; nothing may read "latest" inside a decision path.
- **Claims, observations, inferences are different tables.** Do not store a company's announcement as an
  observation.
- **Identities are checks, not forecasts.** Accounting-identity mechanisms (`A*`) raise on residuals beyond
  tolerance; they never "explain" a residual.
- **No generic edges.** Relationship types are the closed vocabulary in spec §4.2.
- **Tests never touch the network.** Connectors parse stored fixtures in `tests/fixtures/<source>/`. Mark
  anything that genuinely needs a live call with `@pytest.mark.network` (skipped by default).
- **No secrets in the repo.** API keys and session cookies come from environment variables; document the
  variable name in the connector's docstring.
- **Unofficial sources are tiered.** TradingView-API rows carry `source='tradingview_unofficial'` and
  `quality_flag='unofficial'`; MetaTrader 5 rows are cross-check tier. Neither may be the record source for
  `price_bar` rows used in accounting or thesis gates (spec §9.6 tiering).
- **Parameters live in data, not code.** Mechanism parameters and their provenance go in `mechanisms/*.yaml`
  or `config/`; code reads them.
- **No fabricated magnitudes.** Where the spec gives a literature range, use it with the citation; where it
  says "estimate locally", leave the parameter `null` with a `source: TODO_estimate` note and make the
  mechanism refuse to run without it.

## Working in this repo

- Python ≥ 3.12 (3.14 is what the owner's machine runs). Keep dependencies to what `pyproject.toml` lists; if
  you need another package, say so in your final report instead of editing `pyproject.toml`.
- Each ticket owns one package directory under `macrosys/` and the matching `tests/<package>/`. Do not edit
  other packages' files; if you need an interface change in `macrosys/core/`, describe it in your report.
- Pydantic v2 models for all records; DuckDB for tables; Parquet for bulk; plain functions over classes where
  a class adds nothing.
- Every public function has a one-paragraph docstring that names the spec section it implements.
- Run `python -m pytest tests/<your package>` and `ruff check macrosys/<your package>` before reporting.
- Commit messages: imperative, one line, no author tags, e.g. `Add ALFRED vintage parser (D0.2)`.

## Claim labels (used in docs and docstrings)

`[M]` established mechanics / verified provider fact · `[E]` empirical regularity with literature ·
`[H]` hypothesis · `[J]` design judgment. Carry them into code comments when a non-obvious choice is made.
