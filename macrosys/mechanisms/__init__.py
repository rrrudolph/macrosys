"""Mechanism library, composition, and counterfactual checks (spec §3, §10.1)."""

from macrosys.mechanisms.arrays import ArrayQuantity
from macrosys.mechanisms.compose import (
    AcceptedCompeting,
    Loop,
    MechanismInstance,
    PathValidation,
    PropagationPath,
    PropagationRun,
    Propagator,
    Shock,
    StoppedBranch,
    UnitConverter,
    UnstableLoop,
    Wire,
    validate_path,
)
from macrosys.mechanisms.counterfactual import check_monotone
from macrosys.mechanisms.errors import (
    CompetingClosureError,
    IdentityViolationError,
    MissingParameterError,
    UnitContinuityError,
)
from macrosys.mechanisms.eval import bind_parameters, evaluate
from macrosys.mechanisms.examples import load_example
from macrosys.mechanisms.library import Library, load_library
from macrosys.mechanisms.registry import get_impl, mechanism
from macrosys.mechanisms.spec import MechanismSpec

__all__ = [
    "AcceptedCompeting",
    "ArrayQuantity",
    "CompetingClosureError",
    "IdentityViolationError",
    "Library",
    "Loop",
    "MechanismInstance",
    "MechanismSpec",
    "MissingParameterError",
    "PathValidation",
    "Propagator",
    "PropagationPath",
    "PropagationRun",
    "Shock",
    "StoppedBranch",
    "UnitContinuityError",
    "UnitConverter",
    "UnstableLoop",
    "Wire",
    "bind_parameters",
    "check_monotone",
    "evaluate",
    "get_impl",
    "load_example",
    "load_library",
    "mechanism",
    "validate_path",
]
