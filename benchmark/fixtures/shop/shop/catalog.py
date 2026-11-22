from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    name: str


def lookup(products: Iterable[Product], sku: str) -> Product | None:
    return next((product for product in products if product.sku == sku), None)

