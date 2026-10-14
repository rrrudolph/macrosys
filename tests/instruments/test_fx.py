import math

import numpy as np
import pandas as pd
import pytest

from macrosys.instruments import (
    FxConversionRule,
    compose_base_return,
    convert_series,
    decompose_base_return,
)


def test_compose_and_decompose_are_inverses_and_keep_the_cross_term():
    assert set(FxConversionRule) == {
        FxConversionRule.ECB_1600CET,
        FxConversionRule.FED_H10_NOON,
        FxConversionRule.VENDOR_CLOSE,
    }
    # 1.10 * 1.05 - 1 = 0.155, not the sum 0.15.
    assert compose_base_return(0.10, 0.05) == pytest.approx(0.155)
    assert decompose_base_return(0.155, 0.05) == pytest.approx(0.10)
    local = np.array([0.01, -0.02, 0.0, 0.2])
    fx = np.array([0.03, 0.04, -0.01, 0.0])
    base = compose_base_return(local, fx)
    assert decompose_base_return(base, fx) == pytest.approx(local)
    assert compose_base_return(decompose_base_return(base, fx), fx) == pytest.approx(base)

    series = pd.Series([0.10, np.nan], index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    fx_series = pd.Series([0.05, 0.01], index=series.index)
    composed = compose_base_return(series, fx_series)
    assert composed.iloc[0] == pytest.approx(0.155)
    assert math.isnan(composed.iloc[1])
    restored = decompose_base_return(composed, fx_series)
    assert restored.iloc[0] == pytest.approx(0.10)
    assert math.isnan(restored.iloc[1])
    with pytest.raises(ValueError, match="inverse"):
        decompose_base_return(0.0, -1.0)


def test_convert_series_multiplies_and_keeps_the_rule():
    values = pd.Series(
        [100.0, 200.0, 300.0],
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        name="px",
    )
    fx = pd.Series([1.1, 1.2], index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    out = convert_series(values, fx, FxConversionRule.ECB_1600CET)
    assert out.iloc[0] == pytest.approx(110.0)
    assert out.iloc[1] == pytest.approx(240.0)
    assert math.isnan(out.iloc[2])
    assert out.iloc[2] != pytest.approx(360.0)
    assert out.name == "px"
    assert out.attrs["fx_conversion_rule"] is FxConversionRule.ECB_1600CET
    assert out.attrs["provenance"]["spec"] == "§9.1"
    assert out.attrs["provenance"]["fx_conversion_rule"] == "ECB_1600CET"
    assert out.attrs["provenance"]["operation"] == "values * fx"
