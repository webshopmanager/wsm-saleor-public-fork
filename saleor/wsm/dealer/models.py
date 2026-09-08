# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer's own tables. Core Saleor tables are never altered; FKs into them are.

`tier_group` in wsm.compose (`DealerTierOptionPrice.tier_group`) and
`DealerGroup.code` here are the SAME namespace: one string names one buyer group
across option-value deltas and variant tier prices. Compose stores it as a bare
CharField because Compose's pricing function takes a group name and no database;
this app owns the row the name points at.
"""

from django.db import models


class DealerGroup(models.Model):
    """A buyer group: "dealer-1", "warehouse", "installer"."""

    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        return self.name or self.code


class DealerCustomer(models.Model):
    """The link from a signed-in shopper to their group.

    OneToOne: a shopper buys at one group's prices or none. Two groups would
    make "the dealer price" ambiguous on every line, and 5.0 never had it.
    """

    user = models.OneToOneField(
        "account.User", related_name="wsm_dealer", on_delete=models.CASCADE
    )
    group = models.ForeignKey(
        DealerGroup, related_name="customers", on_delete=models.PROTECT
    )
    tax_exempt = models.BooleanField(default=False)

    class Meta:
        ordering = ("pk",)

    def __str__(self):
        return f"{self.user_id} @ {self.group_id}"


class TierPrice(models.Model):
    """One quantity break: this group pays `amount` each from `min_quantity` up.

    An absolute price, not a discount off retail, which is what makes 2.4
    enforceable: there is nothing here for a voucher to combine with.
    """

    variant = models.ForeignKey(
        "product.ProductVariant", related_name="wsm_tier_prices", on_delete=models.CASCADE
    )
    group = models.ForeignKey(
        DealerGroup, related_name="tier_prices", on_delete=models.CASCADE
    )
    min_quantity = models.PositiveIntegerField(default=1)
    # Matched to CheckoutLine.price_override so the number round-trips onto the
    # line without a quantize step that could move a cent.
    amount = models.DecimalField(max_digits=12, decimal_places=3)

    class Meta:
        ordering = ("variant_id", "group_id", "min_quantity")
        constraints = [
            models.UniqueConstraint(
                fields=["variant", "group", "min_quantity"],
                name="wsm_dealer_one_row_per_break",
            )
        ]

    def __str__(self):
        return f"{self.group_id} x{self.min_quantity}: {self.amount}"


class DealerSettings(models.Model):
    """Per-instance toggles. One row, or none.

    No row means the defaults, so a fresh install is default-deny without a data
    migration: `discount_stacking` off is requirement 2.4's ruling.
    """

    discount_stacking = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "dealer settings"

    def __str__(self):
        return "Dealer settings"

    @classmethod
    def stacking_enabled(cls) -> bool:
        row = cls.objects.first()
        return bool(row and row.discount_stacking)
