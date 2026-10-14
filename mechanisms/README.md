# Mechanism library (data, not code)

One YAML file per mechanism, named `<ID>_<slug>.yaml` (e.g. `B03_bottleneck_pricing.yaml`). The schema is
spec §3.1; the reference cards are spec §3.3; the composition rules the engine enforces are spec §3.4.

Rules:

- `class` is one of `identity`, `empirical`, `pricing` and drives engine behaviour (spec §3.2).
- Every input and output names a unit from `macrosys.core.units`.
- Every mechanism lists `competing` IDs, at least one `evidence` observable, and a concrete `falsification`.
- Parameters carry `source`; literature ranges cite the reference key in `docs/spec.md` Appendix A; anything
  to be estimated locally is `null` with `source: TODO_estimate` and the mechanism refuses to run.
- A mechanism without a test under `tests/mechanisms/` is not loaded in CI.
