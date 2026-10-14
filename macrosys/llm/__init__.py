"""Language-model layer: four bounded roles (spec §8.3, §8.4, §9.7, §10.4).

Roles extract shocks, propose mechanism paths, critique a thesis, and narrate an approved
thesis. Providers speak HTTP. Guardrails keep evidence from issuing instructions, keep roles
on an approved snapshot, and keep trade authority out of this package.
"""

from macrosys.llm.anonymize import AnonymizationMap, Anonymizer, deanonymize
from macrosys.llm.cost import (
    BudgetExceededError,
    BudgetGuard,
    CostLedger,
    LedgerRow,
    LlmConfig,
    load_llm_config,
)
from macrosys.llm.guard import (
    EVIDENCE_RULE,
    GuardLog,
    GuardRejectedError,
    OutputPolicy,
    PathCheck,
    Toolkit,
    evidence_block,
)
from macrosys.llm.provider import (
    AnthropicProvider,
    CompletionRequest,
    CompletionResult,
    ContentBlock,
    OpenAIProvider,
    Provider,
    ProviderError,
)
from macrosys.llm.retrieval import Retriever
from macrosys.llm.roles import (
    Narrative,
    NarrativeNumberError,
    Objection,
    PathProposal,
    ShockCandidate,
    critique_thesis,
    extract_shocks,
    narrate,
    propose_paths,
)
from macrosys.llm.trace import GuardEvent, RoleTrace

__all__ = [
    "EVIDENCE_RULE",
    "AnonymizationMap",
    "Anonymizer",
    "AnthropicProvider",
    "BudgetExceededError",
    "BudgetGuard",
    "CompletionRequest",
    "CompletionResult",
    "ContentBlock",
    "CostLedger",
    "GuardEvent",
    "GuardLog",
    "GuardRejectedError",
    "LedgerRow",
    "LlmConfig",
    "Narrative",
    "NarrativeNumberError",
    "Objection",
    "OpenAIProvider",
    "OutputPolicy",
    "PathCheck",
    "PathProposal",
    "Provider",
    "ProviderError",
    "Retriever",
    "RoleTrace",
    "ShockCandidate",
    "Toolkit",
    "critique_thesis",
    "deanonymize",
    "evidence_block",
    "extract_shocks",
    "load_llm_config",
    "narrate",
    "propose_paths",
]
