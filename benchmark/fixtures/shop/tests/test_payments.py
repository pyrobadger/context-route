from decimal import Decimal

from shop.payments import Payment, refund


def test_partial_refund_keeps_captured_amount():
    payment = Payment(Decimal("40.00"))
    assert refund(payment, Decimal("12.00")) == Payment(Decimal("40.00"), Decimal("12.00"))

