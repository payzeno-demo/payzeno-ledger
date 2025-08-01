"""Per-event handler functions.

Kept as free functions rather than methods so the consumer class stays a dispatch table
and each handler's dependencies are visible in its own signature. The consumer supplies
the session; the handler never opens one.
"""

from app.consumers.handlers.merchants import (
    handle_bank_account_verified,
    handle_merchant_created,
    handle_merchant_status_changed,
    handle_merchant_updated,
)
from app.consumers.handlers.payments import (
    handle_dispute_closed,
    handle_dispute_opened,
    handle_payment_authorized,
    handle_payment_canceled,
    handle_payment_captured,
    handle_refund_created,
)

__all__ = [
    "handle_bank_account_verified",
    "handle_dispute_closed",
    "handle_dispute_opened",
    "handle_merchant_created",
    "handle_merchant_status_changed",
    "handle_merchant_updated",
    "handle_payment_authorized",
    "handle_payment_canceled",
    "handle_payment_captured",
    "handle_refund_created",
]
