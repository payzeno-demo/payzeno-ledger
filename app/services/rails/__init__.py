"""Payout rails.

Four ``PayoutInitiator`` concretes plus the one puller that moves money the other way.
``PayoutService`` holds them in a ``{method: initiator}`` map built in
``app/container.py``; nothing outside that map selects a rail.
"""

from app.services.rails.ach import AchPayoutInitiator, SameDayAchPayoutInitiator
from app.services.rails.debit_ach import AchPayoutPuller, DebitPullResult
from app.services.rails.faster_payments import FasterPaymentsPayoutInitiator
from app.services.rails.sepa import SepaPayoutInitiator

__all__ = [
    "AchPayoutInitiator",
    "AchPayoutPuller",
    "DebitPullResult",
    "FasterPaymentsPayoutInitiator",
    "SameDayAchPayoutInitiator",
    "SepaPayoutInitiator",
]
