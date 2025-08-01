"""Dispute and chargeback postings.

The disputed amount and the chargeback fee behave differently and therefore sit in
different accounts (`domain-model.md` §5):

===================  =======================  ========================  ================
Leg                  Debit                    Credit                    Reversed on won?
===================  =======================  ========================  ================
disputed amount      ``merchant_payable``     ``chargeback_liability``  yes
chargeback fee       ``merchant_payable``     ``platform_fee_revenue``  no
===================  =======================  ========================  ================

The fee is charged by the network and **retained regardless of who wins**. Booking it
into ``chargeback_liability`` would mean the ``won`` reversal hands it back to the
merchant, which no acquirer does.
"""

from __future__ import annotations

from typing import ClassVar

from payzeno_contracts.types import LedgerPurpose

from app.domain.postings import PostingContext, PostingLine, PostingRule, credit, debit
from app.errors import ValidationError


class DisputePostingRule(PostingRule):
    """A dispute was opened against a captured charge.

    Two legs on the debit side, both against ``merchant_payable``, because the merchant
    loses the disputed amount *and* the fee the moment the issuer raises it. The amount
    comes back on ``won`` (a ``reversal`` of the liability leg only); the fee never does.

    ``ctx.fee_minor`` is looked up from ``dispute_fee_schedule`` by the caller, with
    :data:`app.domain.fees.DEFAULT_DISPUTE_FEE_BY_CURRENCY` as the fallback. It is not a
    constant: 1500 JPY is about $10 and 1500 USD cents is $15.
    """

    purpose: ClassVar[LedgerPurpose] = "dispute"

    purpose: ClassVar[LedgerPurpose] = "dispute"

    purpose: ClassVar[LedgerPurpose] = "dispute"

