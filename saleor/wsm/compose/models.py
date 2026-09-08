# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Compose's own tables. Core Saleor tables are never altered; FKs into them are.

Every table here is created by this app's migrations under the `wsm_compose`
app label, which is where the `wsm_compose_` name prefix comes from: Django's
default table naming already carries it, so no model spells out a db_table.
"""

from decimal import Decimal

from django.db import models

from . import pricing

PROMPT_TYPE_CHOICES = [(p, p) for p in pricing.PROMPT_TYPES]
FEE_BASIS_CHOICES = [(b, b) for b in pricing.FEE_BASES]
FEE_SCOPE_CHOICES = [(s, s) for s in pricing.FEE_SCOPES]

_CENT = Decimal("0.01")


def to_cents(amount) -> int:
    """Two-decimal currency to integer cents. Exact where a float is not."""
    return int(Decimal(amount).quantize(_CENT) * 100)


class OptionSet(models.Model):
    """One question a product asks, and the answers it accepts."""

    product = models.ForeignKey(
        "product.Product", related_name="wsm_option_sets", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=250)
    label = models.CharField(max_length=250, blank=True)
    prompt_type = models.CharField(
        max_length=20, choices=PROMPT_TYPE_CHOICES, default=pricing.CHOICE_ONE
    )
    required = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "pk")

    def __str__(self):
        return self.label or self.name

    def to_pricing(self) -> pricing.OptionSet:
        # ponytail: reads self.values, so a caller pricing many sets prefetches
        # ("values", "values__tier_deltas") or pays N+1. The one query that
        # loads a whole product's configuration belongs in the view (U2), not here.
        return pricing.OptionSet(
            id=self.pk,
            name=self.name,
            label=self.label,
            prompt_type=self.prompt_type,
            required=self.required,
            note=self.note,
            sort_order=self.sort_order,
            values=tuple(v.to_pricing() for v in self.values.all()),
        )


class OptionValue(models.Model):
    """One answer. `price_delta` is SIGNED: a credit subtracts (requirement 1.1).

    A required $0 value is just a delta of 0; there is no separate flag for it.
    """

    option_set = models.ForeignKey(
        OptionSet, related_name="values", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=250)
    sku_fragment = models.CharField(max_length=100, blank=True)
    price_delta = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    image_url = models.URLField(max_length=500, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "pk")

    def __str__(self):
        return self.name

    def to_pricing(self) -> pricing.Value:
        return pricing.Value(
            id=self.pk,
            name=self.name,
            sku_fragment=self.sku_fragment,
            price_delta=to_cents(self.price_delta),
            sort_order=self.sort_order,
            tier_deltas=tuple(t.to_pricing() for t in self.tier_deltas.all()),
        )


class DealerTierOptionPrice(models.Model):
    """One buyer group's delta for one option value (requirement 2.2).

    Resolved server-side from the customer, never accepted from the browser.
    """

    option_value = models.ForeignKey(
        OptionValue, related_name="tier_deltas", on_delete=models.CASCADE
    )
    tier_group = models.CharField(max_length=100)
    price_delta = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ("tier_group", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=["option_value", "tier_group"],
                name="wsm_compose_one_tier_row_per_group",
            )
        ]

    def __str__(self):
        return f"{self.tier_group}: {self.price_delta}"

    def to_pricing(self) -> pricing.TierDelta:
        return pricing.TierDelta(
            tier_group=self.tier_group, price_delta=to_cents(self.price_delta)
        )


class Fee(models.Model):
    """A charge the merchant attaches to a product: crating, oversize, hazmat, a core deposit.

    Not an option set with one required value: it asks the shopper nothing unless
    it is declinable, it carries its own SKU and label, and it lands on its own
    order line, which is what 5.0's product_fee did.
    """

    product = models.ForeignKey(
        "product.Product", related_name="wsm_fees", on_delete=models.CASCADE
    )
    label = models.CharField(max_length=250)
    sku = models.CharField(max_length=100, blank=True)
    basis = models.CharField(
        max_length=10, choices=FEE_BASIS_CHOICES, default=pricing.FIXED
    )
    # Money when basis is fixed, a percentage when it is percent.
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    apply_to = models.CharField(
        max_length=10, choices=FEE_SCOPE_CHOICES, default=pricing.PER_UNIT
    )
    required = models.BooleanField(default=True)
    decline_label = models.CharField(max_length=250, blank=True)
    # The hidden variant this fee's checkout line points at (U2). Fee is our
    # table, so the column is ours; the FK reaches into a core table, which the
    # design allows, and no core table is altered.
    variant = models.ForeignKey(
        "product.ProductVariant",
        related_name="+",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        ordering = ("pk",)

    def __str__(self):
        return self.label

    def to_pricing(self) -> pricing.Fee:
        return pricing.Fee(
            id=self.pk,
            label=self.label,
            sku=self.sku,
            basis=self.basis,
            amount=to_cents(self.amount),
            apply_to=self.apply_to,
            required=self.required,
            decline_label=self.decline_label,
        )

    def ensure_variant(self, channel):
        return _ensure_fee_variant(self, channel)


# Fee variants already built in this process, as (fee id, channel id). The
# channel listings behind a fee variant are created once and never deleted, so
# re-checking them on every configured add would be two queries bought for
# nothing. ponytail: a process-lifetime memo, not a cache with a TTL; the
# ceiling is that deleting a fee's channel listing by hand needs a restart.
_ENSURED_FEE_VARIANTS: set[tuple[int, int]] = set()


def _ensure_fee_variant(fee, channel):
    """The hidden variant a fee's checkout line points at, created on first use.

    A fee has to be its own checkout line to reach the order with its own SKU and
    label, which is what 5.0's product_fee did and what the ERP export reads. A
    line needs a variant, so each fee owns one: product type "WSM Fee", one
    product, one variant, one channel listing at zero. The listing is a
    placeholder Saleor requires, never a price: the money is always the
    price_override the view computes.
    """
    from django.utils import timezone

    from ...product import ProductTypeKind
    from ...product.models import (
        Product,
        ProductChannelListing,
        ProductType,
        ProductVariant,
        ProductVariantChannelListing,
    )

    if (fee.pk, channel.pk) in _ENSURED_FEE_VARIANTS and fee.variant_id:
        return fee.variant

    variant = fee.variant
    if variant is None:
        product_type, _ = ProductType.objects.get_or_create(
            slug="wsm-fee",
            defaults={
                "name": "WSM Fee",
                "kind": ProductTypeKind.NORMAL,
                "has_variants": False,
                "is_shipping_required": False,
            },
        )
        # Two shoppers configuring the same product in the same second both
        # arrive here, and the first add of a fee is a shopper request that
        # writes catalog rows. Keyed on the identifiers that are unique in the
        # database, so the loser of the race is handed the row the winner made
        # instead of an IntegrityError on the way to a 500.
        product, _ = Product.objects.get_or_create(
            slug=f"wsm-fee-{fee.pk}",
            defaults={"product_type": product_type, "name": fee.label},
        )
        # The SKU is ours and namespaced, never the merchant's own fee SKU: SKU
        # is unique across the whole catalog, so taking `fee.sku` here either
        # collides with the merchant's real product of that SKU or silently
        # claims a code the ERP means for something else. The merchant's SKU
        # still reaches the order, on the parent line's `wsm.options` snapshot
        # (`fees[].sku`), which is what the export reads.
        variant, _ = ProductVariant.objects.get_or_create(
            sku=f"wsm-fee-{fee.pk}",
            defaults={
                "product": product,
                "name": fee.label[:255],
                "track_inventory": False,
            },
        )
        fee.variant = variant
        fee.save(update_fields=["variant"])

    now = timezone.now()
    ProductChannelListing.objects.get_or_create(
        product_id=variant.product_id,
        channel=channel,
        defaults={
            "is_published": True,
            "published_at": now,
            # Never in a listing and never in search: a fee is not something a
            # shopper can find, only something a line can point at.
            "visible_in_listings": False,
            "available_for_purchase_at": now,
            "currency": channel.currency_code,
            "discounted_price_amount": Decimal("0"),
        },
    )
    ProductVariantChannelListing.objects.get_or_create(
        variant=variant,
        channel=channel,
        defaults={
            "currency": channel.currency_code,
            "price_amount": Decimal("0"),
        },
    )
    _ENSURED_FEE_VARIANTS.add((fee.pk, channel.pk))
    return variant
