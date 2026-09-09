# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""One rounding rule for every price this fork charges.

The rule was written three times in two modes: `compose/models.to_cents`
quantized with Python's default ROUND_HALF_EVEN, `containers/pricing.to_cents`
and `dealer/pricing.to_money` both passed ROUND_HALF_UP, and `reprice.py`
imported the HALF_EVEN one. So the funnel that decided the final order total
rounded a half cent DOWN while the ladder that quoted it to the shopper rounded
UP: 8.005 was quoted at 8.01 and charged at 8.00.

HALF_UP wins because it is the number already on the shopper's screen, and a
charge that is a cent under a quote is the merchant's loss on every line.
Saleor's own `quantize_price` is HALF_EVEN and stays where it is: it rounds
totals Saleor computed, and this rounds the amounts we hand Saleor.

Nothing here touches the database or Django, so it imports from nowhere in the
app and every module below can import it without a cycle.
"""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_money(amount) -> Decimal:
    """An amount as the money that will actually be charged, to the cent."""
    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


def to_cents(amount) -> int:
    """The same rounding, as integer cents. Exact where a float is not."""
    return int(to_money(amount) * 100)
