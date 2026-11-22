def validate_quantity(quantity: int) -> None:
    if not isinstance(quantity, int):
        raise ValueError("Quantity must be an integer")

