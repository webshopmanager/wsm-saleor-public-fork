# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Kit money, with no database in sight.

Every expected number is written out by hand from the spec, never derived from
the code under test: a proration that agrees with itself proves nothing.
"""

from decimal import Decimal

import pytest

from saleor.wsm.containers import pricing


class FakeVariant:
    """Just an identity. `pricing` never reads a variant, it only passes it on."""

    def __init__(self, pk):
        self.pk = pk

    def __repr__(self):
        return f"<variant {self.pk}>"


def member(pk, price, quantity=1):
    return pricing.Member(
        variant=FakeVariant(pk),
        unit_list_cents=pricing.to_cents(price),
        quantity=quantity,
    )


def test_proration_is_exact_across_three_uneven_members():
    """19.99 + 45.00 + 7.77 = 72.76, less 10 percent = 65.48, to the cent.

    By hand: the discount is 7.276 rounded to 7.28. The exact shares are 1.9995,
    4.5025 and 0.7774; the floors are 1.99, 4.50 and 0.77, which is 7.26, so two
    cents are left over and go to the two largest remainders (0.9549 on the
    19.99 member, 0.7426 on the 7.77 member), not to the largest member.
    """
    kit = [member(1, "19.99"), member(2, "45.00"), member(3, "7.77")]

    priced = pricing.price_kit(kit, pricing.PERCENT, Decimal(10))

    assert priced.list_total_cents == 7276
    assert priced.discount_cents == 728
    assert [line.unit_cents for line in priced.lines] == [1799, 4050, 699]
    assert priced.total_cents == 6548
    # The invariant the merchant cares about: the lines ARE the kit total.
    assert (
        sum(line.unit_cents * line.line_quantity for line in priced.lines)
        == priced.list_total_cents - priced.discount_cents
    )


def test_shares_are_the_list_price_shares():
    kit = [member(1, "19.99"), member(2, "45.00"), member(3, "7.77")]

    priced = pricing.price_kit(kit, pricing.PERCENT, Decimal(10))

    assert [line.share for line in priced.lines] == ["0.27", "0.62", "0.11"]


def test_largest_remainder_tie_goes_to_the_first_of_the_equals():
    """Three identical members and ten cents: 3.33 each, one cent left over.

    Every remainder is identical, so the tie-break has to be deterministic or
    the same cart prices two ways on two servers.
    """
    kit = [member(1, "10.00"), member(2, "10.00"), member(3, "10.00")]

    priced = pricing.price_kit(kit, pricing.FIXED, Decimal("0.10"))

    assert [line.unit_cents for line in priced.lines] == [996, 997, 997]
    assert priced.total_cents == 2990


def test_residue_follows_the_largest_remainder_not_the_largest_line():
    """The leftover cent goes on remainder, so the small line can take it."""
    kit = [member(1, "10.00"), member(2, "20.00")]
    # 30.00 at a 0.03 discount: exact shares 0.01 and 0.02, no residue. Nudge it
    # to 0.05: shares 1.666 and 3.333 cents, floors 1 and 3, remainders .666 and
    # .333, so the cent goes to the 10.00 line on remainder, not on size.
    priced = pricing.price_kit(kit, pricing.FIXED, Decimal("0.05"))

    assert [line.unit_cents for line in priced.lines] == [998, 1997]
    assert priced.total_cents == 2995


def test_dealer_takes_the_tier_and_the_kit_discount_does_not_stack():
    """Requirement 2.3: better of, never both."""
    kit = [member(1, "100.00"), member(2, "50.00")]
    tiers = {1: Decimal("80.00")}

    def tier_lookup(variant, user, quantity):
        return tiers.get(variant.pk)

    priced = pricing.price_kit(
        kit, pricing.PERCENT, Decimal(10), tier_lookup=tier_lookup
    )

    tiered, retail = priced.lines
    # 80.00 flat: NOT 80.00 less the 10.00 the proration would have given it.
    assert tiered.unit_cents == 8000
    assert tiered.on_tier is True
    # The member with no tier keeps its own prorated retail kit price.
    assert retail.unit_cents == 4500
    assert retail.on_tier is False
    assert priced.total_cents == 12500


def test_dealer_never_pays_more_than_retail():
    """A tier above the kit-discounted retail unit is refused, silently and correctly."""
    kit = [member(1, "100.00"), member(2, "50.00")]

    def tier_lookup(variant, user, quantity):
        return Decimal("95.00") if variant.pk == 1 else None

    priced = pricing.price_kit(
        kit, pricing.PERCENT, Decimal(10), tier_lookup=tier_lookup
    )

    assert priced.lines[0].unit_cents == 9000
    assert priced.lines[0].on_tier is False


def test_kit_quantity_multiplies_lines_not_prices():
    kit = [member(1, "100.00"), member(2, "50.00", quantity=2)]

    priced = pricing.price_kit(kit, pricing.FIXED, Decimal("20.00"), kit_quantity=3)

    # 100.00 + 2 x 50.00 = 200.00 a kit; 20.00 off, prorated half and half.
    assert [line.unit_cents for line in priced.lines] == [9000, 4500]
    assert [line.line_quantity for line in priced.lines] == [3, 6]
    assert priced.total_cents == 54000  # 9000 x 3 + 4500 x 6


def test_a_discount_that_eats_the_kit_is_refused():
    kit = [member(1, "10.00")]

    with pytest.raises(pricing.KitRefusal):
        pricing.price_kit(kit, pricing.FIXED, Decimal("10.00"))


def test_an_empty_kit_is_refused():
    with pytest.raises(pricing.KitRefusal):
        pricing.price_kit([], pricing.FIXED, Decimal("1.00"))


# --- verdict "KitMember quantity 0 and prorate residue" ----------------------


def test_a_member_holding_none_of_its_variant_is_refused_not_a_crash():
    """`prorate` divides by the member quantity: 0 came out as ZeroDivisionError.

    A merchant typing 0 into a kit line read as a 500 on the product page rather
    than as the bad kit row it is. `KitMember.quantity` refuses it at the screen
    now; this is the money's own guard, for the rows a screen never touched.
    """
    kit = [member(1, "10.00", quantity=0), member(2, "10.00")]

    with pytest.raises(pricing.KitRefusal):
        pricing.price_kit(kit, pricing.PERCENT, Decimal(10))


def test_a_discount_no_member_quantity_can_carry_is_reported_as_nothing_taken():
    """Two 1.00 units on one member, 0.01 off: no unit price can carry half a cent.

    The unit discount is a per-unit number, so 1 cent over a member of quantity
    2 allocates nothing. The kit used to report `discount_cents` of 1 against a
    total that had not moved, so `list_total - discount` and the sum of the lines
    disagreed by a cent: an order that says it discounted money it charged.
    """
    kit = [member(1, "1.00", quantity=2)]

    priced = pricing.price_kit(kit, pricing.FIXED, Decimal("0.01"))

    assert priced.list_total_cents == 200
    assert priced.total_cents == 200
    assert priced.discount_cents == 0
    assert priced.list_total_cents - priced.discount_cents == priced.total_cents
