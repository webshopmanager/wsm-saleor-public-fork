# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Money on the wire, because stock's money scalar is a float.

`saleor/graphql/core/scalars.py:49` declares `PositiveDecimal` as a
`graphene.Float` and never overrides `serialize`, so a Decimal leaves the server
as an IEEE double: `Decimal("228.000")` goes out as `228.0`, and a screen that
round-trips what it was handed can post back a number nobody typed. A price is
an exact string here, trailing zeros and all.

`parse_value` is the second half of the same argument. `PositiveDecimal` maps a
below-zero amount to None, and None reaches every mutation as "you did not say
what this group pays" (REQUIRED) instead of "a dealer price is at least one
cent". A scalar that nulls a number it dislikes throws away the one fact the
error message needed, so this one keeps the sign and leaves the refusing to the
rule that owns the column.
"""

from ...graphql.core.scalars import Decimal, PositiveDecimal


class WsmDecimal(PositiveDecimal):
    """An exact decimal on the way out, the typed number on the way in."""

    @staticmethod
    def serialize(value):
        return None if value is None else str(value)

    parse_value = Decimal.parse_value
    parse_literal = Decimal.parse_literal
