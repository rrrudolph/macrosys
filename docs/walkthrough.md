# Walking skeleton

One command runs the whole decision loop on made-up numbers, writes the artefacts, and stops at
no-trade. A second command runs that same loop on three different shocks. The shocks differ. The
code does not.

The numbers are stubs. Each one is labelled `stub:S0.1`. They are there so the loop can be
replayed. They are not forecasts, and they are not a reading of any market.

## Run it

From the repository root, with the virtualenv installed:

```powershell
.\.venv\Scripts\python -m macrosys skeleton run --shock demand --as-of 2026-10-14 --out out\demand
.\.venv\Scripts\python -m macrosys skeleton demo --out out\demo
.\.venv\Scripts\python -m macrosys ledger verify out\demand\ledger.jsonl
.\.venv\Scripts\python -m macrosys library list
.\.venv\Scripts\python -m macrosys library validate
```

`--shock` is `demand`, `fx`, or `transport`. `--as-of` is a calendar day. The demo always uses
14 October 2026, so two runs of the demo match.

`skeleton run` writes three files into `--out`:

| File | What it is |
|---|---|
| `report.md` | One page: the shock, the mechanism path (units and horizons), identity residuals, the gap, which gate failed, the ledger hash, and the manifest digest. |
| `manifest.json` | A fingerprint of the stub database, the mechanism-library hash, the parameter hash, and the skeleton label `S0.1`. |
| `ledger.jsonl` | The thesis, frozen. Each line carries the hash of the previous line. `ledger verify` recomputes that chain. |

`skeleton demo` writes an `index.md` plus one directory per shock (`demand`, `fx`, `transport`),
each with its own report, manifest, and ledger.

The same inputs produce the same run digest. The digest is the hash of the manifest digest and
the ledger line's hash. A second run has to start from an empty ledger: appending to a ledger
that already has a line changes the chain, on purpose.

## What the three shocks do

All three go through one function. A table keyed by shock type picks the mechanism path. Nothing
else in the run looks at the shock type.

| Shock | What it stands in for | Path | Why it does not trade |
|---|---|---|---|
| `demand` | Contracted investment, in the style of an AI-capex boom (spec §12.1). | Supply-use identity `A04`, then bottleneck pricing `B03`. | The gap between the propagated price change and a stub "already priced" range includes zero. |
| `fx` | A 15% currency depreciation, the sketch in spec §12.3. | Exchange-rate pass-through `B16`. | `B16` refuses to run. Its pass-through coefficients are still `TODO_estimate`. The thesis records that as blocking missing evidence. |
| `transport` | A supply disruption that draws inventories (spec §12.5). | `A04`, then the inventory cycle `B04`, then `B03`. | Same as demand: the gap includes zero. |

The demand shock's size is 15 billion USD of investment, and the transport shock's size is a
5 billion USD inventory change. Those are the levels that make the supply-use identity close in
the stub table. They are not an estimate of capex or of a chokepoint. The bridge from a dollar
flow to a percent price change is a stub coefficient with the same label, because those
mechanisms do not share a unit.

`B03` will not run while its elasticities are `TODO_estimate`. The demand and transport paths
fill those in with the example scenario already stored on the `B03` record, relabelled
`stub:S0.1`. That is a stand-in so the loop can finish. It is not an estimate. `B16` is left
empty so the refusal is visible.

The stub world behind every run is five estimated series: capacity utilization 0.80, inventory
cover 45 days, a cost share 0.18, a policy rate 4.5 percent, and an FX rate 5.40. Publication is
the day before `as-of`. Ingestion is `as-of` itself (or a clock you pass in). Quality is
`estimated`.

## Where each step sits in the spec

| What the run does | Spec |
|---|---|
| Shock record: type, stage, size, cutoff | §4.1 |
| Which mechanisms run first for that type | §4.1, and the three sketches in §12.1, §12.3, §12.5 |
| Stub observations with three timestamps, read as of a fixed instant | §9.2, §4.3 |
| World-state rows the mechanisms can see | §4.3 |
| Path check: units, horizons, competing effects | §3.4 |
| Propagation, including an identity residual | §3.4 rule 7. An identity that does not close fails the run. |
| A parameter that is still `TODO_estimate` refuses to run | §3.1. The FX pass records this instead of inventing a coefficient. |
| Thesis: expected change, priced-in range, gap = expected − priced | §7.7, §7.3 |
| Four gates, no-trade unless all of them pass | §11.3, and the loop in §1.1 |
| Weekly decision, nothing sent to a broker | §11.2 |
| Frozen forecast ledger | §10.2 |
| Manifest of the database and the library version | §4.4 |
| Same inputs, same digest | §4.4 |
| One command; then the same command on three shocks | §13 S0.1 and S0.2 |

The expression gate fails as well, because holdings exposure is not computed in this pass. The
recorded failure is the earlier one: the gap, or the missing `B16` parameter. No order is sent.
