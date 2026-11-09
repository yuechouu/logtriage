"""Demo order domain objects (used only by the logtriage examples).

The file deliberately keeps the same layout as the deployed service that
produced examples/demo.log, so line numbers in the log resolve back here.
"""

from __future__ import annotations

from decimal import Decimal

from .db import get_connection
from .pricing import apply_discount


class OrderError(Exception):
    """Raised when an order cannot be processed."""


def calculate_total(items: list[dict], discount_code: str | None = None) -> Decimal:
    total = Decimal("0")
    for item in items:
        price = Decimal(str(item["price"]))
        quantity = int(item["quantity"])
        total += price * quantity
    if discount_code:
        total = apply_discount(total, discount_code)
    return total


def load_order(order_id: int) -> dict:
    connection = get_connection()
    row = connection.execute(
        "SELECT id, user_id, status FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if row is None:
        raise OrderError(f"order {order_id} not found")
    return dict(row)
