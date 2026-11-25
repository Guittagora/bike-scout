"""Domain model shared by the scraper, storage, sheet and notifier."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Listing:
    url: str
    title: str
    brand: str = ""
    price: float | None = None
    price_label: str = ""
    market_price: float | None = None
    size: str = ""
    groupset: str = ""
    country: str = ""
    favorites: int = 0
    hot_deal: bool = False
    sold: bool = False
    image_url: str | None = None
    description: str = ""
    published: str | None = None  # ISO date (YYYY-MM-DD) when known

    @property
    def discount(self) -> float | None:
        """Fraction below market price (0.25 == 25% cheaper), if computable."""
        if self.price is None or not self.market_price:
            return None
        return 1 - self.price / self.market_price


_NUMBER_RE = re.compile(r"\d[\d.,']*")


def parse_price(value: object) -> float | None:
    """Turn ``1500``, ``"€ 1.500"``, ``"1,234.50 €"`` or ``"1.234,50"`` into a float.

    Thousands/decimal separators are inferred: when both ``.`` and ``,`` appear
    the last one is the decimal separator; when only one appears it is treated
    as a thousands separator if exactly three digits follow it.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER_RE.search(str(value))
    if not match:
        return None
    number = match.group(0).replace("'", "").rstrip(".,")
    if "." in number and "," in number:
        decimal = "." if number.rfind(".") > number.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        number = number.replace(thousands, "").replace(decimal, ".")
    elif "." in number or "," in number:
        sep = "." if "." in number else ","
        head, _, tail = number.rpartition(sep)
        if len(tail) == 3:
            number = number.replace(sep, "")
        else:
            number = head.replace(sep, "") + "." + tail
    try:
        return float(number)
    except ValueError:
        return None
