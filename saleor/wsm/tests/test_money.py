# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""One rounding rule, proved at every door money enters this fork through."""

from decimal import Decimal

from .. import reprice
from ..compose.models import to_cents as compose_to_cents
from ..containers.pricing import to_cents as containers_to_cents
from ..dealer.pricing import to_money as dealer_to_money
from ..money import to_cents, to_money

# The one input that told the three implementations apart: HALF_EVEN sends it
# down to 8.00 and HALF_UP takes it up to 8.01.
HALF_CENT = Decimal("8.005")


def test_a_half_cent_rounds_the_same_way_through_every_entry_point():
    """The funnel used to charge 8.00 for what the ladder quoted at 8.01."""
    assert to_money(HALF_CENT) == Decimal("8.01")
    assert to_cents(HALF_CENT) == 801
    assert compose_to_cents(HALF_CENT) == 801
    assert containers_to_cents(HALF_CENT) == 801
    assert dealer_to_money(HALF_CENT) == Decimal("8.01")
    # The order-total funnel imports the compose name; it is the same function.
    assert reprice.to_cents is to_cents


def test_a_half_cent_credit_rounds_away_from_zero_too():
    """A credit is money as well: -8.005 credits 8.01, not 8.00."""
    assert to_cents(-HALF_CENT) == -801
    assert compose_to_cents(-HALF_CENT) == -801
    assert containers_to_cents(-HALF_CENT) == -801
