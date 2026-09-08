# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The checks `checkoutLinesAdd` runs, for the fork's own write endpoints.

Every fork write goes through `add_variants_to_checkout`, which is the same
function the stock mutation calls, but the stock mutation validates FIRST and
the fork endpoints did not: a variant that is unpublished, not available for
purchase, not listed in the channel, out of stock, or past
`limit_quantity_per_checkout` went straight into the cart. Nothing here is
reimplemented; these are the stock validators, imported and called in the order
`CheckoutLinesAdd.clean_input` calls them, so an upstream rule change reaches the
fork endpoints for free.

The one thing that IS ours is the shape of the refusal: a REST endpoint answers
422 with `violations`, not a GraphQL error list.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError

from ..checkout.error_codes import CheckoutErrorCode
from ..graphql.checkout.mutations.utils import (
    check_lines_quantity,
    get_variants_and_total_quantities,
    validate_variants_are_published,
    validate_variants_available_for_purchase,
)
from ..graphql.core.validators import validate_variants_available_in_channel
from ..warehouse.reservations import is_reservation_enabled


class LineRefused(Exception):
    """A write the stock validators would have refused. Carries their messages."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


def check_addable(
    checkout, channel, variants, lines_data, *, site_settings, delivery_method_info=None
):
    """Raise `LineRefused` if `checkoutLinesAdd` would have refused this write.

    ponytail: `existing_lines` is not passed, so the stock check weighs only the
    quantity in THIS call, not the cart's running total for the same variant.
    That matches what these endpoints do today (each posts a new line) and costs
    no extra query; the upgrade is to hand it `fetch_checkout_lines(checkout)`
    once these endpoints start topping up an existing line.
    """
    counted_variants, quantities = get_variants_and_total_quantities(
        variants, lines_data
    )
    try:
        check_lines_quantity(
            counted_variants,
            quantities,
            checkout.get_country(),
            channel.slug,
            site_settings.limit_quantity_per_checkout,
            delivery_method_info=delivery_method_info,
            check_reservations=is_reservation_enabled(site_settings),
            calculate_stocks_with_shipping_zones=(
                site_settings.use_legacy_shipping_zone_stock_availability
            ),
        )
        variant_ids = {variant.id for variant in counted_variants}
        if variant_ids:
            validate_variants_available_for_purchase(variant_ids, channel.id)
            validate_variants_available_in_channel(
                variant_ids,
                channel.id,
                CheckoutErrorCode.UNAVAILABLE_VARIANT_IN_CHANNEL.value,
            )
            validate_variants_are_published(variant_ids, channel.id)
    except ValidationError as error:
        raise LineRefused(list(error.messages)) from error


def whole_number(raw, default=1):
    """The posted quantity as an int, or None when it is not a number at all.

    `int("x")` is a 500 on a request a caller can send at will; the endpoints
    turn a None from here into a 422.
    """
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
