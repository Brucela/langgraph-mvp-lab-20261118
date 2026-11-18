"""Small price utilities for the private integration lab."""


def subtotal(prices_cents: list[int]) -> int:
    return sum(prices_cents)


def apply_discount(price_cents: int, discount_percent: int) -> int:
    """Apply a whole-percent discount and round the resulting price down."""
    if price_cents < 0:
        raise ValueError("price_cents must be nonnegative")
    if not 0 <= discount_percent <= 100:
        raise ValueError("discount_percent must be between 0 and 100")
    return price_cents * (100 - discount_percent) // 100
