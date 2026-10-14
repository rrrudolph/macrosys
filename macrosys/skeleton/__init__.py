"""Walking skeleton: one decision-loop pass on stub data (spec §13 S0.1, S0.2)."""

from macrosys.skeleton.demos import DEMAND, DEMO_SHOCKS, FX, TRANSPORT, run_demo, shock_by_name
from macrosys.skeleton.run import SkeletonResult, run_once
from macrosys.skeleton.stubs import STUB_PROVENANCE

__all__ = [
    "DEMAND",
    "DEMO_SHOCKS",
    "FX",
    "STUB_PROVENANCE",
    "TRANSPORT",
    "SkeletonResult",
    "run_demo",
    "run_once",
    "shock_by_name",
]
