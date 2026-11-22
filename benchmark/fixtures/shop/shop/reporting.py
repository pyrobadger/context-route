from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from .money import round_currency
from .orders import Order


def sales_total(orders: Iterable[Order], day: date) -> Decimal:
    total = sum((order.amount for order in orders if order.status == "paid"), Decimal(0))
    return round_currency(total)

