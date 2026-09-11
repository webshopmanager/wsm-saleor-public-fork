# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The two places the gate is ENFORCED, both server side, both default deny.

MP6, the price. `Product.pricing` and `ProductVariant.pricing` are the only two
public surfaces that hand a shopper a number, and both are already declared
NULLABLE and already return None when a listing is missing
(`saleor/graphql/product/types/products.py:697,1249`). So a gated product is
answered with the null stock already promises, and no storefront needs a new
shape to render it. The wrapper short-circuits BEFORE the original runs, so it
depends on nothing inside that function and does not carry a source digest the
way MP1 to MP3 do: what it depends on is the field being nullable, and that is
asserted here at install rather than pinned as a hash that an unrelated upstream
line would redden.

Rebound at `ready()`, before any schema is built, so BOTH the stock schema and
the composed one this fork serves carry the guard. Doing it the way MP4 does it
(between the two builds) would have gated only the schema we serve, and a gate
that depends on which URL matched first is not default deny.

MP7, the purchase. `add_variants_to_checkout` is the one function every write
into a cart goes through: `checkoutCreate`, `checkoutLinesAdd`,
`checkoutLinesUpdate` (which subclasses it), `checkoutCreateFromOrder`, and the
fork's own REST endpoints, which call it directly. Wrapping it once is why there
is no per-mutation copy of this rule. The buyer is `checkout.user_id`, which IS
the requester on a cart: an anonymous checkout has none.

Only lines that ADD quantity are weighed. A shopper whose cart already holds a
line the merchant has since gated must still be able to take it OUT, and
`checkoutLinesUpdate` removes by sending quantity 0 through this same function.

WHAT THIS COSTS. Nothing per product: see `gate.py`. On a cart write, two
queries on a store that has gated nothing, and one more for the buyer's group
only when something might be. On a browse, the dataloader's two.
"""

from __future__ import annotations

import functools

from django.core.exceptions import ImproperlyConfigured

from .. import patches
from . import gate

PRICING_RESOLVERS = (
    "saleor.graphql.product.types.products.Product.resolve_pricing",
    "saleor.graphql.product.types.products.ProductVariant.resolve_pricing",
)


def _product_id(root):
    """The product this resolver was asked about, from either root type.

    `Product`'s root node IS the product; `ProductVariant`'s carries the id as a
    column, so neither reads a row to answer this.
    """
    node = root.node
    return getattr(node, "product_id", None) or node.pk


def _guard_pricing(original):
    @functools.wraps(original)
    def resolve_pricing(root, info, **kwargs):
        # No channel is stock's own "there is no pricing to show" case, and it
        # is answered without a gate lookup: nothing is disclosed either way.
        if not root.channel_slug:
            return original(root, info, **kwargs)
        from ..graphql.dealer.dataloaders import ProductGatedLoader

        return (
            ProductGatedLoader(info.context)
            .load(_product_id(root))
            .then(lambda gated: None if gated else original(root, info, **kwargs))
        )

    return resolve_pricing


def _guard_add_variants(original):
    @functools.wraps(original)
    def add_variants_to_checkout(checkout, variants, checkout_lines_data, *args, **kw):
        gate.refuse_gated_lines(checkout, variants, checkout_lines_data)
        return original(checkout, variants, checkout_lines_data, *args, **kw)

    return add_variants_to_checkout


def _assert_pricing_is_nullable():
    """The one thing MP6 assumes, checked where it is assumed.

    A `pricing` field that ever became non-null would turn every gated product
    into a GraphQL error instead of a blank price, which is a worse failure than
    the leak this exists to stop. Cheap: two attribute reads at boot.
    """
    import graphene

    from ...graphql.product.types.products import Product, ProductVariant

    for owner in (Product, ProductVariant):
        field = owner._meta.fields["pricing"]
        if isinstance(field.type, graphene.NonNull):
            raise ImproperlyConfigured(
                f"{owner.__name__}.pricing is no longer nullable, so the gated "
                "catalogue cannot answer it with null. Read the upstream diff "
                "and see saleor/wsm/dealer/gate_enforce.py."
            )


def install():
    """MP6 and MP7. Called from `DealerConfig.ready()`."""
    _assert_pricing_is_nullable()
    for name in PRICING_RESOLVERS:
        patches.install_method_guard(name, _guard_pricing)
    patches.install_guard(
        "saleor.checkout.utils.add_variants_to_checkout", _guard_add_variants
    )
