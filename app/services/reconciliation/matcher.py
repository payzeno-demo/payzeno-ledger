"""Matching acquirer settlement lines to the charges we think they belong to.

Four strategies, tried in a fixed order, first hit wins. The order is not arbitrary: it
runs from "the acquirer told us the reference we gave them" down to "these two numbers
are close enough that a human should look". A line that no strategy claims stays
``unmatched`` with ``charge_id = NULL`` and becomes an ``orphaned`` item at posting time
rather than being force-fitted onto a plausible charge.
"""

from __future__ import annotations

import abc
from datetime import timedelta
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models.reconciliation_item import ReconciliationItem
from app.ports import Clock
from app.repositories.settlement_charge import SettlementChargeRepository
from app.services.reconciliation.types import MatchOutcome

logger = get_logger(__name__)

#: How far either side of the acquirer's processing date a heuristic match may look.
HEURISTIC_WINDOW = timedelta(days=2)

#: Minor units of slack the heuristic strategy tolerates on gross amount.
HEURISTIC_AMOUNT_SLACK_MINOR = 2

NO_MATCH = MatchOutcome(charge_id=None, method="unmatched", confident=False)


class MatchStrategy(abc.ABC):
    """One way of deciding which charge a settlement line belongs to."""

    method: ClassVar[str]

    @abc.abstractmethod
    async def match(
        self, session: AsyncSession, item: ReconciliationItem
    ) -> MatchOutcome:
        """Return a :class:`MatchOutcome`; ``NO_MATCH`` when this strategy abstains."""


class ExactReferenceMatch(MatchStrategy):
    """``acquirer_reference == settlement_charge.processor_reference``.

    The primary key of reconciliation. It is the value payzeno-api sent the acquirer at
    authorisation and the value the acquirer echoes back on the settlement line, and it
    is backed by ``ix_settlement_charge_processor_reference``.
    """

    method = "exact_reference"

    def __init__(self, charges: SettlementChargeRepository) -> None:
        self._charges = charges

    async def match(
        self, session: AsyncSession, item: ReconciliationItem
    ) -> MatchOutcome:
        charge = await self._charges.find_by_processor_reference(
            session, acquirer=item.acquirer, processor_reference=item.acquirer_reference
        )
        if charge is None:
            return NO_MATCH
        return MatchOutcome(charge_id=charge.charge_id, method=self.method, confident=True)


class NetworkTransactionMatch(MatchStrategy):
    """Fall back to the card network's own transaction id.

    Worldflow occasionally rewrites its reference for representments but never rewrites
    the network transaction id, so this catches chargeback and reversal lines that the
    exact-reference strategy misses.
    """

    method = "network_transaction"

    def __init__(self, charges: SettlementChargeRepository) -> None:
        self._charges = charges

    async def match(
        self, session: AsyncSession, item: ReconciliationItem
    ) -> MatchOutcome:
        if not item.network_reference:
            return NO_MATCH
        charge = await self._charges.find_by_network_transaction(
            session, acquirer=item.acquirer, network_transaction_id=item.network_reference
        )
        if charge is None:
            return NO_MATCH
        return MatchOutcome(charge_id=charge.charge_id, method=self.method, confident=True)


class HeuristicAmountWindowMatch(MatchStrategy):
    """Amount + merchant + date-window. Produces ``needs_review``, never ``settled``.

    Deliberately not confident. Two charges of the same amount for the same merchant on
    the same day are ordinary, and settling the wrong one moves real money.
    """

    method = "heuristic_amount_window"

    def __init__(self, charges: SettlementChargeRepository, clock: Clock) -> None:
        self._charges = charges
        self._clock = clock

    async def match(
        self, session: AsyncSession, item: ReconciliationItem
    ) -> MatchOutcome:
        if item.merchant_id is None:
            return NO_MATCH
        now = self._clock.now()
        candidates = await self._charges.find_in_amount_window(
            session,
            merchant_id=item.merchant_id,
            currency=item.currency,
            amount_minor=item.gross_minor,
            slack_minor=HEURISTIC_AMOUNT_SLACK_MINOR,
            authorized_from=now - HEURISTIC_WINDOW,
            authorized_to=now + HEURISTIC_WINDOW,
        )
        if len(candidates) != 1:
            # Zero is no evidence; more than one is ambiguous evidence. Both abstain.
            return NO_MATCH
        return MatchOutcome(
            charge_id=candidates[0].charge_id, method=self.method, confident=False
        )


class ManualMatch(MatchStrategy):
    """Applied by an operator through ``POST /internal/v1/ops/items/{itemId}/match``.

    Never reached by :func:`match_items` — it is invoked directly with a charge id that
    a human chose, and it exists as a strategy so ``reconciliation_item.match_method``
    records how the link was made.
    """

    method = "manual"

    def __init__(self, charges: SettlementChargeRepository) -> None:
        self._charges = charges

    async def match(
        self, session: AsyncSession, item: ReconciliationItem
    ) -> MatchOutcome:
        if item.charge_id is None:
            return NO_MATCH
        charge = await self._charges.get_or_raise(session, item.charge_id)
        return MatchOutcome(charge_id=charge.charge_id, method=self.method, confident=True)


async def match_items(
    session: AsyncSession,
    items: list[ReconciliationItem],
    strategies: list[MatchStrategy],
    *,
    clock: Clock,
) -> int:
    """Walk every item through the strategies in order; stop at the first hit.

    Returns the number of items that gained a ``charge_id``. Mutates the items in place
    inside the caller's transaction — the import service and the ops route both already
    hold one.
    """
    matched = 0
    for item in items:
        outcome = NO_MATCH
        for strategy in strategies:
            outcome = await strategy.match(session, item)
            if outcome.charge_id is not None:
                break

        if outcome.charge_id is None:
            item.match_method = "unmatched"
            continue

        item.charge_id = outcome.charge_id
        item.match_method = outcome.method
        item.matched_at = clock.now()
        if not outcome.confident:
            item.status = "needs_review"
        matched += 1

    logger.info("reconciliation_match_pass", total=len(items), matched=matched)
    return matched
