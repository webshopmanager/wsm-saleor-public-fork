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

`unit_amount` is here for the same reason: WHICH of a channel listing's two
prices the fork charges from is one question with one answer, and it was being
answered differently in four places.

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


def unit_amount(listing):
    """What one of a variant costs in a channel, the merchant's sale included.

    A `ProductVariantChannelListing` carries two prices: `price_amount` is the
    list price a merchant typed, and `discounted_price_amount` is that price less
    whatever catalogue promotion they then put the product on. Saleor's own
    pricing answers with the second one and the storefront shows it, so a fork
    price computed from the first charges list for something the shop is
    advertising on sale. Measured 2026-09-09 on a live kit member: base 2154.00,
    promotion 254.01, Saleor quoting 1899.99, and the kit endpoint charging
    2154.00.

    The fallback is not a guess. `discounted_price_amount` is NULL only until the
    first price recalculation has run over a listing, and a listing that has
    never been recalculated has no promotion on it to honour.

    Duck-typed on purpose: a listing row, a listing loaded with `.only()`, and a
    test stub all answer the same two attributes, so this stays importable
    without Django like the rest of this module.
    """
    discounted = listing.discounted_price_amount
    return listing.price_amount if discounted is None else discounted
