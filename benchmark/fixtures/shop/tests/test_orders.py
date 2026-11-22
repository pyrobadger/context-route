from decimal import Decimal

from shop.orders import checkout


def test_checkout_total():
    assert checkout(Decimal("9.50"), 2) == Decimal("24.00")


def test_checkout_with_free_shipping():
    assert checkout(Decimal("30.00"), 2) == Decimal("60.00")

