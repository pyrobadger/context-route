from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .pricing import line_total
from .shipping import shipping_fee


@dataclass(frozen=True)
class Order:
    order_id: str
    ordered_on: date
    amount: Decimal
    status: str


def checkout(unit_price: Decimal, quantity: int) -> Decimal:
    subtotal = line_total(unit_price, quantity)
    return subtotal + shipping_fee(subtotal)

