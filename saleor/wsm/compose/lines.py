# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""What the fork stamps on a checkout line, and the one fee line both paths write.

The keys are contract, not detail: the storefront cart and the order screens
read the PUBLIC copies, and MP3 (saleor/wsm/reprice.py) prices from the PRIVATE
ones. They live here rather than inside a view because two write paths now share
them, the configured line the compose endpoint adds and the kit the containers
endpoint explodes, and a second copy of a metadata key is how two carts start
disagreeing about what a line is.

`fee_line` is the same story in code. A fee reaches the order as its OWN line so
it carries its own SKU and label, which is what 5.0's product_fee did; the line
points at the fee's hidden variant, is priced from our own tables, and says what
it belongs to. That was written inside the compose view, and then a kit member
carrying a fee needed exactly the same line.
"""

import json
from decimal import Decimal

from ...core.utils.metadata_manager import MetadataItem
from ...graphql.checkout.mutations.utils import CheckoutLineData

META_OPTIONS = "wsm.options"
META_SKU = "wsm.options.sku"
META_CID = "wsm.options.cid"
META_ACCEPTED = "wsm.options.acc"
META_FEE = "compose.fee"
META_PARENT = "compose.parent_line"

PRICE_OVERRIDE_REASON = "wsm.compose"

# The keys MP3 prices from. Their authority copy is written to PRIVATE metadata
# (stock Saleor lets any unauthenticated caller write PUBLIC line metadata:
# saleor/graphql/meta/permissions.py maps CheckoutLine to `no_permissions`), and
# the public copies stay because the storefront cart and order screens read them
# for display and B6 is "no storefront code".
PRICED_FROM = (META_OPTIONS, META_CID, META_ACCEPTED, META_FEE, META_PARENT)


def fee_line(fee, row, channel, *, cid, quantity, extra_metadata=()):
    """Build the hidden variant and the checkout line for ONE charged fee.

    `row` is the fee as the pricing engine reported charging it
    (`compose.pricing.apply_fees`), so no amount here ever came from a caller.
    `quantity` is the LINE's: a per-unit fee is one line of the parent's
    quantity and a per-line fee is one of 1, and the fee's own unit price is the
    same either way, so the line total is the fee total that was quoted.

    The parent is named by its `cid`, never by its line pk: the storefront pairs
    a fee to its item on `compose.parent_line == the parent's wsm.options.cid`
    (composeFee.ts feeLinesFor) and on nothing else. A pk here orphaned every
    fee in the cart AND left it out of withChildLines, so a per-unit crate
    charge stayed at one unit's money when the shopper bought two. The cid also
    makes the pairing knowable BEFORE the insert.
    """
    variant = fee.ensure_variant(channel)
    metadata = [
        MetadataItem(
            META_FEE,
            json.dumps({"label": row["label"], "apply_to": row["apply_to"]}),
        ),
        MetadataItem(META_CID, cid),
        MetadataItem(META_PARENT, cid),
        *extra_metadata,
    ]
    return variant, CheckoutLineData(
        variant_id=str(variant.pk),
        quantity=quantity,
        quantity_to_update=True,
        custom_price=Decimal(row["amount"]) / 100,
        custom_price_to_update=True,
        custom_price_reason=PRICE_OVERRIDE_REASON,
        custom_price_reason_to_update=True,
        metadata_list=metadata,
    )


def private_stamps(line_data, extra=None):
    """Take the copy MP3 prices from off a line this process just built.

    Only the priced-from keys, and only from a line we wrote ourselves:
    promoting whatever happens to be in a line's public metadata would honour
    exactly the forgery the private copy exists to close.
    """
    stamps = {
        item.key: item.value
        for item in line_data.metadata_list
        if item.key in PRICED_FROM
    }
    if extra:
        stamps.update(extra)
    return stamps
