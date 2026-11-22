from decimal import Decimal

from .money import round_currency


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    return unit_price * quantity


def apply_discount(subtotal: Decimal, percent: int) -> Decimal:
    rate = Decimal(percent) / 100
    return round_currency(subtotal * (1 - rate))

