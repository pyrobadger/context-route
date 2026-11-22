from decimal import Decimal

from .money import round_currency


def receipt_line(name: str, value: Decimal) -> str:
    return f"{name}: ${round_currency(value):.2f}"

