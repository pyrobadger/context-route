from dataclasses import dataclass


@dataclass(frozen=True)
class Stock:
    sku: str
    on_hand: int
    reserved: int = 0


def availability(item: Stock) -> int:
    """Return stock available for new orders."""
    return item.on_hand


def can_fulfill(item: Stock, quantity: int) -> bool:
    return 0 < quantity <= availability(item)

