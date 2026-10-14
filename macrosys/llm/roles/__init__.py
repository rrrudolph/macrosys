"""Language-model roles 1–4 (spec §8.3)."""

from macrosys.llm.roles.critique import Objection, critique_thesis
from macrosys.llm.roles.extract import ShockCandidate, SourceDocument, SourceSpan, extract_shocks
from macrosys.llm.roles.narrate import MonitoringDelta, Narrative, NarrativeNumberError, narrate
from macrosys.llm.roles.propose import PathProposal, ProposedStep, propose_paths

__all__ = [
    "MonitoringDelta",
    "Narrative",
    "NarrativeNumberError",
    "Objection",
    "PathProposal",
    "ProposedStep",
    "ShockCandidate",
    "SourceDocument",
    "SourceSpan",
    "critique_thesis",
    "extract_shocks",
    "narrate",
    "propose_paths",
]
