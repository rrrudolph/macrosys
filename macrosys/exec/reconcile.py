"""Daily ledger-versus-broker reconciliation (spec §11.7).

``Reconciler`` flags every difference above tolerance as unexplained. This function then splits
those breaks. A difference no larger than the notional still working (a pending fill) is a class-B
note: the cycle can be reviewed, and submission is not halted. Anything left over is a class-A
break: an incident is written and ``HaltTrading`` is raised so no new order goes out.
"""

from __future__ import annotations

from collections.abc import Mapping

from macrosys.decide.portfolio import Reconciler, ReconciliationBreak, ReconciliationReport
from macrosys.exec.broker import BrokerAdapter
from macrosys.exec.oms import HaltTrading, OrderManager, pending_cover


def daily_reconciliation(oms: OrderManager, broker: BrokerAdapter, tol: float) -> ReconciliationReport:
    """Compare OMS fills with ``broker.positions()`` and ``broker.cash()`` (spec §11.7).

    ``tol`` is absolute USD notional (or USD cash). Pending fills explain a break only when the
    absolute difference, minus the still-open notional on that instrument (or USD cash), is within
    ``tol``. Unexplained breaks raise ``HaltTrading`` after the incident row is committed. Explained
    breaks are class-B notes and the returned report is clear.
    """
    report = Reconciler.compare(oms.positions(), broker.positions(), oms.cash(), broker.cash(), tol)
    if report.ok:
        return report
    by_instrument, cash_cover = pending_cover(oms.open_orders())
    explained: list[ReconciliationBreak] = []
    unexplained: list[ReconciliationBreak] = []
    for item in report.breaks:
        if _explained(item, by_instrument, cash_cover, tol):
            explained.append(item)
        else:
            unexplained.append(item)
    for item in explained:
        oms.record_incident("B", "pending_fill", _detail("pending fill explains", item))
    if unexplained:
        detail = "; ".join(_detail("unexplained", item) for item in unexplained)
        oms.record_incident("A", "reconciliation_break", detail)
        raised = ReconciliationReport(ok=False, class_a=True, breaks=unexplained)
        raise HaltTrading(detail, report=raised)
    return ReconciliationReport(ok=True, class_a=False, breaks=[])


def _explained(
    item: ReconciliationBreak,
    by_instrument: Mapping[str, float],
    cash_cover: float,
    tol: float,
) -> bool:
    cover = 0.0
    if item.kind == "position":
        cover = by_instrument.get(item.key, 0.0)
    elif item.kind == "cash" and item.key == "USD":
        cover = cash_cover
    if cover <= 0:
        return False
    return abs(item.difference) - cover <= tol


def _detail(prefix: str, item: ReconciliationBreak) -> str:
    return (
        f"{prefix} {item.kind} {item.key}: ledger {item.ledger:.2f} broker {item.broker:.2f} "
        f"difference {item.difference:.2f} USD"
    )
