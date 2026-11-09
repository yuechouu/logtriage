"""Discount rules for the demo order service."""

from decimal import Decimal

DISCOUNTS = {
    "SAVE10": Decimal("0.10"),
    "SAVE20": Decimal("0.20"),
}


def apply_discount(total: Decimal, code: str) -> Decimal:
    rate = DISCOUNTS.get(code)
    if rate is None:
        raise ValueError(f"unknown discount code: {code}")
    return (total * (Decimal("1") - rate)).quantize(Decimal("0.01"))
