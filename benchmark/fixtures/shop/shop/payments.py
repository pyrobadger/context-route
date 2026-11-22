from dataclasses import dataclass, replace
from decimal import Decimal


@dataclass(frozen=True)
class Payment:
    captured: Decimal
    refunded: Decimal = Decimal("0.00")


def refund(payment: Payment, amount: Decimal) -> Payment:
    if amount <= 0:
        raise ValueError("Refund must be positive")
    if amount > payment.captured:
        raise ValueError("Refund exceeds payment balance")
    return replace(payment, refunded=payment.refunded + amount)

