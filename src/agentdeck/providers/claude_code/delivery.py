"""Compatibility exports for the provider-neutral keyed-worker delivery fence."""

from ..delivery import (
    DELIVERY_RECEIPT_CAP,
    DELIVERY_RECEIPT_TTL_S,
    DeliverResult,
    DeliveryReceipts,
)

__all__ = [
    "DELIVERY_RECEIPT_CAP",
    "DELIVERY_RECEIPT_TTL_S",
    "DeliverResult",
    "DeliveryReceipts",
]
