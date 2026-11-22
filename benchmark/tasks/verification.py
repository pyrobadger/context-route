"""Run an individual acceptance test from a verification copy of the checkout."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

import pytest

D = Decimal
TODAY = date(2026, 1, 4)
YESTERDAY = date(2026, 1, 3)


def test_availability():
    from shop.inventory import Stock, availability, can_fulfill

    assert availability(Stock("BOARD", 8, 3)) == 5
    assert availability(Stock("BOARD", 2, 5)) == 0
    assert not can_fulfill(Stock("BOARD", 8, 3), 6)


def test_discount():
    from shop.pricing import apply_discount

    assert apply_discount(D("50"), 120) == D("0.00")
    assert apply_discount(D("50"), -10) == D("50.00")
    assert apply_discount(D("50"), 15) == D("42.50")


def test_refund():
    from shop.payments import Payment, refund

    payment = refund(Payment(D("100")), D("65"))
    with pytest.raises(ValueError):
        refund(payment, D("36"))
    assert refund(payment, D("35")).refunded == D("100")


def test_report():
    from shop.orders import Order
    from shop.reporting import sales_total

    orders = (Order("A", TODAY, D("21.125"), "paid"), Order("B", YESTERDAY, D("17.00"), "paid"),
              Order("C", TODAY, D("20.00"), "cancelled"))
    assert sales_total(iter(orders), TODAY) == D("21.13")


def test_shipping():
    from shop.shipping import shipping_fee

    assert shipping_fee(D("50")) == 0
    assert shipping_fee(D("49.99")) == 5


def test_lookup():
    from shop.catalog import Product, lookup

    product = Product("   bOX-1 ", "Storage box")
    assert lookup([product], " box-1 ") == product
    assert lookup([product], "box-2") is None


def test_express():
    from shop.orders import checkout
    from shop.shipping import shipping_fee

    assert checkout(D("31"), 2, express=True) == D("74")
    assert checkout(D("9"), 1, express=True) == D("21")
    assert checkout(D("31"), 2) == D("62")
    assert shipping_fee(D("62"), express=True) == D("12")


def test_quantities():
    from shop.orders import checkout
    from shop.pricing import line_total
    from shop.validation import validate_quantity

    for value in (0, -1, "2", True, 2.5):
        for method in (checkout, line_total):
            with pytest.raises(ValueError):
                method(D("10"), value)
        with pytest.raises(ValueError):
            validate_quantity(value)
    assert checkout(D("10"), 2) == D("25")


def test_restock():
    from shop.inventory import Stock, low_stock

    items = [Stock("B", 8, 7), Stock("A", 5, 2), Stock("C", 2, 5)]
    assert low_stock(items, 3) == ["B", "C"]
    assert low_stock(items, 0) == []


def test_receipts():
    from shop.money import format_money
    from shop.receipts import receipt_line

    assert format_money(D("12.125")) == "$12.13"
    assert receipt_line("Order", D("12.125")) == "Order: $12.13"


def test_agent_regression():
    root = Path.cwd()
    test_path = root / "tests" / "test_quantity_regression.py"
    assert test_path.is_file()
    actual = subprocess.run([sys.executable, "-m", "pytest", "-q", str(test_path)], cwd=root, capture_output=True)
    assert actual.returncode == 0, actual.stdout.decode(errors="replace")
    module = root / "shop" / "validation.py"
    original = module.read_text(encoding="utf-8")
    try:
        module.write_text("def validate_quantity(quantity):\n    return None\n", encoding="utf-8")
        mutant = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", str(test_path)], cwd=root, capture_output=True)
        assert mutant.returncode == 1, mutant.stdout.decode(errors="replace")
    finally:
        module.write_text(original, encoding="utf-8")


def test_export():
    from shop.orders import Order
    from shop.reporting import export_rows

    orders = [Order("B", TODAY, D("5.125"), "paid"), Order("A", TODAY, D("21"), "paid"),
              Order("C", YESTERDAY, D("17"), "paid"), Order("D", TODAY, D("20"), "cancelled")]
    assert export_rows(iter(orders), TODAY) == [("A", D("21.00")), ("B", D("5.13"))]

