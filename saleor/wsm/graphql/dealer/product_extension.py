# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Two more fields on stock's `Product`, for the gated catalogue.

Same seam and same ledger entry as MP4's three compose fields. The appending is
done by `compose/product_extension.append_product_fields`, imported here, so the
ORDERING that makes MP4 safe (stock's own schema is built first, by the
`saleor.graphql.api` import at the top of that module, which runs before the
call at the bottom of this one) holds for these two as well, and there is one
`_assert_free` and one `_wsm_owned` marker in the layer rather than two.
`patches.EXTENDED` pins five field names now rather than three.

`wsmGated` is PUBLIC, and has to be. The gate already answers the price surfaces
with null server side; a storefront that saw only the null could not tell "this
store has no price for YOU" from "this product is not sold in this channel", and
would render a blank where it should render "Sign in for dealer pricing". One
boolean, resolved off the same per-request loader the price guard uses, so
asking for it on a 100-product listing costs nothing beyond the lookup that
listing already made.

`wsmGate` is the merchant's side of the same row, on MANAGE_DISCOUNTS like every
other read and write in this domain.
"""

import graphene

from ....graphql.core.fields import PermissionsField
from ....permission.enums import DiscountPermissions
from ..compose.product_extension import append_product_fields
from ..types import DOC_CATEGORY_WSM
from .dataloaders import ProductGateByProductIdLoader, ProductGatedLoader
from .types import WsmProductGate

WSM_DEALER_PRODUCT_FIELDS = ("wsm_gated", "wsm_gate")


def _resolve_gated(root, info, **_kwargs):
    return ProductGatedLoader(info.context).load(root.node.id)


def _resolve_gate(root, info, **_kwargs):
    return ProductGateByProductIdLoader(info.context).load(root.node.id)


def extend_product_type_with_gate():
    """Append `wsmGated` and `wsmGate` to stock's `Product`. Idempotent."""
    return append_product_fields(
        {
            "wsm_gated": (
                # A mounted `Field`, not a bare `graphene.Boolean`: a scalar in
                # a class BODY is mounted by the metaclass, and this dict goes
                # straight into `_meta.fields`, where an unmounted one has no
                # `.type` and kills the schema build.
                graphene.Field(
                    graphene.Boolean,
                    required=True,
                    description=(
                        "True when the shopper making this request may not see "
                        "this product's price and may not buy it. The price "
                        "fields are already null for them; this says WHY, so a "
                        "storefront can offer a sign-in instead of a blank."
                    ),
                ),
                _resolve_gated,
            ),
            "wsm_gate": (
                PermissionsField(
                    WsmProductGate,
                    description=(
                        "Who may see this product's price and buy it. Null when "
                        "this product has no rule of its own and the store-wide "
                        "switch decides."
                    ),
                    permissions=[DiscountPermissions.MANAGE_DISCOUNTS],
                    doc_category=DOC_CATEGORY_WSM,
                ),
                _resolve_gate,
            ),
        }
    )


extend_product_type_with_gate()


__all__ = ["WSM_DEALER_PRODUCT_FIELDS", "extend_product_type_with_gate"]
