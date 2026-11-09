"""Payment gateway adapter for the demo project."""

from decimal import Decimal


def charge(amount: Decimal, currency: str = "CNY") -> str:
    # BUG: mixing Decimal with float raises TypeError.
    fee = amount * 0.029 + 0.30
    return f"charged {amount + fee} {currency}"
