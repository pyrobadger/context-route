from decimal import Decimal


def shipping_fee(subtotal: Decimal) -> Decimal:
    if subtotal > Decimal("50.00"):
        return Decimal("0.00")
    return Decimal("5.00")

