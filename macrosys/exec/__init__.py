"""Paper execution: OMS, paper broker, reconciliation, kill switch, and runbook hash (spec §11.7).

Nothing here reads a thesis or chooses a size. It submits validated ``OrderTicket`` values and
halts on class-A failures. A live broker adapter is not included; credentials stay in a separate
process (spec §8.3(c)).
"""

from macrosys.exec.broker import BrokerAdapter, BrokerEvent, BrokerLookup, PaperBroker
from macrosys.exec.cli_commands import register_cli
from macrosys.exec.killswitch import KillSwitch
from macrosys.exec.oms import (
    Fill,
    HaltTrading,
    Incident,
    KillSwitchEngaged,
    ManagedOrder,
    OrderEvent,
    OrderManager,
)
from macrosys.exec.reconcile import daily_reconciliation
from macrosys.exec.runbook import RunbookBlockedError, check_runbook, record_runbook_hash

__all__ = [
    "BrokerAdapter",
    "BrokerEvent",
    "BrokerLookup",
    "Fill",
    "HaltTrading",
    "Incident",
    "KillSwitch",
    "KillSwitchEngaged",
    "ManagedOrder",
    "OrderEvent",
    "OrderManager",
    "PaperBroker",
    "RunbookBlockedError",
    "check_runbook",
    "daily_reconciliation",
    "record_runbook_hash",
    "register_cli",
]
