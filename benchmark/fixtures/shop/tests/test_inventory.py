from shop.inventory import Stock, can_fulfill


def test_can_fulfill_in_stock_order():
    assert can_fulfill(Stock("BOOK", 4), 4)


def test_rejects_empty_orders():
    assert not can_fulfill(Stock("BOOK", 4), 0)

