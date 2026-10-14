"""Historical cases and named capability checks (spec §10.1)."""

from macrosys.cases.capability_tests import assert_capability, diversification_assumption, run_capability
from macrosys.cases.runner import CaseResult, CaseRunner, CaseSuite, SuiteReport
from macrosys.cases.schema import Case, load_case, load_cases

__all__ = [
    "Case",
    "CaseResult",
    "CaseRunner",
    "CaseSuite",
    "SuiteReport",
    "assert_capability",
    "diversification_assumption",
    "load_case",
    "load_cases",
    "run_capability",
]
