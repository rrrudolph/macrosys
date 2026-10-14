"""World-state transforms and the weekly refresh (spec §4.3, §9.2)."""

from datetime import UTC, date, datetime, timedelta

import duckdb
import numpy as np
import pandas as pd

from macrosys.core.contracts import EntityKey, Observation
from macrosys.mechanisms.library import load_library
from macrosys.mechanisms.registry import get_impl
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema
from macrosys.store.state import WorldStateStore
from macrosys.world.recipes import Recipe, load_recipes
from macrosys.world.refresh import refresh_world_state, weekly_refresh

AS_OF = datetime(2024, 6, 15, tzinfo=UTC)
LIBRARY = load_library("mechanisms")


def _obs(
    series_id: str,
    end: date,
    value: float,
    unit: str,
    *,
    published: datetime | None = None,
    vintage: str = "v1",
    quality: str = "ok",
    start: date | None = None,
) -> Observation:
    stamp = published or datetime(2024, 6, 1, tzinfo=UTC)
    return Observation(
        series_id=series_id,
        ref_period_start=start or end,
        ref_period_end=end,
        value=value,
        unit=unit,
        published_at=stamp,
        ingested_at=stamp,
        vintage_id=vintage,
        source_url="https://example.test/fixture",
        source_hash="fixture",
        quality_flag=quality,  # type: ignore[arg-type]
    )


def _recipe(recipe_id: str, var: str, unit: str, inputs: list[str], transform: str, **overrides) -> Recipe:
    payload = {
        "id": recipe_id,
        "status": "ready",
        "entity": {"type": "economy", "id": "USA"},
        "var": var,
        "unit": unit,
        "inputs": inputs,
        "transform": transform,
        "min_quality": ["ok", "estimated", "reconstructed_pubtime"],
        "max_staleness_days": 400,
    }
    payload.update(overrides)
    return Recipe.model_validate(payload)


def _run(rows: list[Observation], recipes: list[Recipe]):
    conn = duckdb.connect(database=":memory:")
    create_schema(conn)
    ObservationStore(conn).append(rows)
    report = refresh_world_state(conn, AS_OF, recipes)
    return conn, report


def _value(conn, var: str, entity_id: str = "USA", entity_type: str = "economy") -> float:
    quantity = WorldStateStore(conn).view(AS_OF).get(EntityKey(entity_type=entity_type, entity_id=entity_id), var)
    assert quantity.value is not None
    return quantity.value


def test_default_recipes_mark_missing_sources():
    recipes = load_recipes()
    awaiting = sorted(recipe.id for recipe in recipes if recipe.status == "awaiting_source")
    assert "equity_absorption_ratio" in awaiting
    assert "usa_equity_variance_regime" in awaiting
    assert "cp_ois_spread" in awaiting
    assert "cip_basis" in awaiting
    assert "capacity_pipeline" in awaiting
    assert any(recipe.id == "usa_capacity_utilization" and recipe.status == "ready" for recipe in recipes)


def test_latest_rescales_capacity_and_respects_publication_time():
    recipe = _recipe("usa_capacity_utilization", "capacity_utilization", "share", ["FRED:TCU"], "latest")
    rows = [
        _obs("FRED:TCU", date(2024, 4, 30), 65.0, "pct", vintage="before", published=datetime(2024, 5, 15, tzinfo=UTC)),
        _obs(
            "FRED:TCU",
            date(2024, 4, 30),
            90.0,
            "pct",
            vintage="after",
            published=datetime(2024, 6, 20, tzinfo=UTC),
        ),
        _obs(
            "FRED:TCU",
            date(2024, 5, 31),
            91.0,
            "pct",
            vintage="later-period",
            published=datetime(2024, 6, 20, tzinfo=UTC),
        ),
    ]
    conn, report = _run(rows, [recipe])
    assert report.skipped == []
    assert _value(conn, "capacity_utilization") == 0.65
    written = report.written[0]
    assert written.quality_flag == "ok"
    assert "recipe:usa_capacity_utilization" in written.derived_from
    assert "FRED:TCU@before" in written.derived_from


def test_stale_input_is_skipped_and_not_written():
    recipe = _recipe(
        "usa_policy_rate",
        "policy_rate",
        "pct",
        ["FRED:DFF"],
        "latest",
        entity={"type": "institution", "id": "FED"},
        max_staleness_days=10,
    )
    old = date(2024, 1, 1)
    conn, report = _run([_obs("FRED:DFF", old, 5.3, "pct")], [recipe])
    assert report.written == []
    assert report.skipped[0].recipe_id == "usa_policy_rate"
    assert report.skipped[0].reason.startswith("stale input")
    view = WorldStateStore(conn).view(AS_OF)
    assert not view.has(EntityKey(entity_type="institution", entity_id="FED"), "policy_rate")


def test_quality_propagation_and_rejection():
    accept = _recipe("levels", "inventory_sales_from_levels", "ratio", ["FRED:BUSINV", "FRED:TOTBUSSMSA"], "ratio(FRED:BUSINV, FRED:TOTBUSSMSA)")
    reject = _recipe(
        "reject_estimated",
        "other_ratio",
        "ratio",
        ["FRED:BUSINV", "FRED:TOTBUSSMSA"],
        "ratio(FRED:BUSINV, FRED:TOTBUSSMSA)",
        min_quality=["ok"],
    )
    end = date(2024, 5, 31)
    rows = [
        _obs("FRED:BUSINV", end, 200.0, "USD_mn", quality="ok"),
        _obs("FRED:TOTBUSSMSA", end, 100.0, "USD_mn", quality="estimated"),
    ]
    _conn, report = _run(rows, [accept, reject])
    written = {row.recipe_id: row for row in report.written}
    assert written["levels"].value == 2.0
    assert written["levels"].quality_flag == "estimated"
    assert [row.recipe_id for row in report.skipped] == ["reject_estimated"]


def test_each_transform():
    end = date(2024, 5, 31)
    earlier = [end - timedelta(days=30 * step) for step in range(1, 6)]
    rows = [
        _obs("FRED:SOFR", end, 5.30, "pct"),
        _obs("FRED:IORB", end, 5.40, "pct"),
        _obs("FRED:BUSINV", end, 300.0, "USD_mn"),
        _obs("FRED:TOTBUSSMSA", end, 100.0, "USD_mn"),
        _obs("FRED:SP500", end, 110.0, "index"),
        _obs("FRED:SP500", earlier[0], 100.0, "index", vintage="p1"),
    ]
    for index, day in enumerate(earlier):
        rows.append(_obs("FRED:ISRATIO", day, float(index + 1), "ratio", vintage=f"z{index}"))
    rows.append(_obs("FRED:ISRATIO", end, 6.0, "ratio", vintage="z-last"))
    rng = np.random.default_rng(0)
    calm = rng.normal(0.0, 0.01, 40)
    storm = rng.normal(0.0, 0.25, 40)
    for index, value in enumerate(np.concatenate([calm, storm])):
        day = date(2024, 5, 31) - timedelta(days=79 - index)
        rows.append(
            _obs("MACROSYS:RET:SP500", day, float(value), "ratio", vintage=f"r{index}", quality="estimated")
        )
        rows.append(
            _obs(
                "MACROSYS:RET:DGS10",
                day,
                float(value * 0.2),
                "ratio",
                vintage=f"d{index}",
                quality="estimated",
            )
        )
    recipes = [
        _recipe(
            "spread",
            "funding_spread",
            "bp",
            ["FRED:SOFR", "FRED:IORB"],
            "spread(FRED:SOFR, FRED:IORB)",
            entity={"type": "institution", "id": "FED"},
        ),
        _recipe(
            "cover",
            "inventory_days_of_cover",
            "d",
            ["FRED:BUSINV", "FRED:TOTBUSSMSA"],
            "days_of_cover(FRED:BUSINV, FRED:TOTBUSSMSA, 30)",
        ),
        _recipe("change", "price_pct_change", "pct", ["FRED:SP500"], "pct_change(FRED:SP500, 1)"),
        _recipe("z", "inventory_zscore", "ratio", ["FRED:ISRATIO"], "zscore(FRED:ISRATIO, 3)"),
        _recipe(
            "absorb",
            "absorption_ratio",
            "share",
            ["MACROSYS:RET:SP500", "MACROSYS:RET:DGS10"],
            "regime_absorption(MACROSYS:RET:SP500, MACROSYS:RET:DGS10, window=20, n_components=1)",
            status="awaiting_source",
            min_quality=["ok", "estimated"],
        ),
        _recipe(
            "varreg",
            "equity_stress_probability",
            "share",
            ["MACROSYS:RET:SP500"],
            "regime_variance(MACROSYS:RET:SP500)",
            status="awaiting_source",
            min_quality=["ok", "estimated"],
        ),
    ]
    conn, report = _run(rows, recipes)
    failed = {item.recipe_id: item.reason for item in report.skipped}
    assert failed == {} or set(failed) <= {"varreg"}, failed
    assert abs(_value(conn, "funding_spread", "FED", "institution") - (-10.0)) < 1e-9
    assert abs(_value(conn, "inventory_days_of_cover") - 90.0) < 1e-9
    assert abs(_value(conn, "price_pct_change") - 10.0) < 1e-9
    z = _value(conn, "inventory_zscore")
    window = pd.Series([2.0, 1.0, 6.0])
    expected = (6.0 - float(window.mean())) / float(window.std(ddof=1))
    assert abs(z - expected) < 1e-9
    absorb = _value(conn, "absorption_ratio")
    assert 0.0 < absorb <= 1.0
    if "varreg" not in failed:
        stress = _value(conn, "equity_stress_probability")
        assert 0.0 <= stress <= 1.0


def test_b03_gate_reads_capacity_utilization():
    recipe = _recipe("usa_capacity_utilization", "capacity_utilization", "share", ["FRED:TCU"], "latest")
    conn, report = _run([_obs("FRED:TCU", date(2024, 5, 31), 65.0, "pct")], [recipe])
    assert report.skipped == []
    state = WorldStateStore(conn).view(AS_OF)
    spec = LIBRARY.get("B03_bottleneck_pricing")
    kink = next(item for item in spec.parameters if item.name == "u_kink")
    from macrosys.core.quantity import Quantity

    gate = get_impl(spec.id).applicable(
        state,
        {"entity_type": "economy", "entity_id": "USA"},
        {"u_kink": Quantity(value=kink.value, unit=kink.unit, provenance=kink.source)},
    )
    assert gate.status == "scaled"
    assert gate.factor == 0.0
    assert "capacity_utilization" in gate.reason


def test_weekly_hook_signature_on_an_empty_store():
    conn = duckdb.connect(database=":memory:")
    create_schema(conn)
    report = refresh_world_state(conn, AS_OF)
    assert report.written == []
    assert len(report.skipped) == len(load_recipes())
    assert weekly_refresh(conn, AS_OF) is None
