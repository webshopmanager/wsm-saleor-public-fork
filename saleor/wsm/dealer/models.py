# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Dealer's own tables. Core Saleor tables are never altered; FKs into them are.

`tier_group` in wsm.compose (`DealerTierOptionPrice.tier_group`) and
`DealerGroup.code` here are the SAME namespace: one string names one buyer group
across option-value deltas and variant tier prices. Compose stores it as a bare
CharField because Compose's pricing function takes a group name and no database;
this app owns the row the name points at.

Owning the name means owning the answer to "is this one real", so this module
exports the two things any screen in that namespace needs, and wsm.compose is
meant to call them rather than keep a second copy of the rule:

    DealerGroup.objects.codes()      -> ["dealer-1", "warehouse"], for choices
    validate_tier_group_code(code)   -> raises ValidationError naming the codes
                                        that do exist, for a form field

A free-text tier group that matches no row is a price that silently never
applies, which is the failure this pair is here to turn into a field error.
"""

from django.core.exceptions import ValidationError
from django.db import models


class DealerGroupQuerySet(models.QuerySet):
    def codes(self):
        """Every buyer-group code, in one query, for a choices list."""
        return list(self.order_by("code").values_list("code", flat=True))


class DealerGroup(models.Model):
    """A buyer group: "dealer-1", "warehouse", "installer"."""

    code = models.CharField(
        max_length=100,
        unique=True,
        help_text=(
            "The exact code that names this buyer group everywhere else: on the "
            "dealer tier rows of an option value, and in the storefront. Short, "
            "lower case, no spaces, and never changed once prices point at it."
        ),
    )
    name = models.CharField(
        max_length=250,
        blank=True,
        help_text="What staff see in lists. Leave blank to show the code.",
    )

    objects = DealerGroupQuerySet.as_manager()

    class Meta:
        ordering = ("code",)

    def __str__(self):
        # Both vocabularies in one string: the dealer screens show the name, the
        # compose screens store and show the code, and a merchant renaming a
        # group has to know they are the same thing.
        return f"{self.name} ({self.code})" if self.name else self.code


def validate_tier_group_code(code):
    """Refuse a buyer-group code that names no `DealerGroup` row.

    A validator, not a constraint: wsm.compose stores the code as a plain string
    on purpose (its pricing function takes a group name and no database), so the
    check belongs on the way in, once, where a merchant can still fix the typo.
    An empty code is left to the caller's own required-ness rule.
    """
    if not code:
        return
    if not DealerGroup.objects.filter(code=code).exists():
        known = ", ".join(DealerGroup.objects.codes()) or "none yet"
        raise ValidationError(
            f"{code!r} is not a dealer group. Add it under Dealer groups first, "
            f"or use one of: {known}."
        )


class DealerCustomer(models.Model):
    """The link from a signed-in shopper to their group.

    OneToOne: a shopper buys at one group's prices or none. Two groups would
    make "the dealer price" ambiguous on every line, and 5.0 never had it.
    """

    user = models.OneToOneField(
        "account.User",
        related_name="wsm_dealer",
        on_delete=models.CASCADE,
        help_text=(
            "The signed-in shopper who buys at this group's prices. Search by "
            "email; the account must already exist in the store."
        ),
    )
    group = models.ForeignKey(
        DealerGroup,
        related_name="customers",
        on_delete=models.PROTECT,
        help_text="The one buyer group whose prices this shopper gets.",
    )
    tax_exempt = models.BooleanField(
        default=False,
        help_text=(
            "Charge this shopper no sales tax. Their exemption certificate is "
            "yours to hold on file; nothing here checks for one."
        ),
    )

    class Meta:
        ordering = ("pk",)

    def __str__(self):
        # `user_id` is a UUID and `group_id` is a row number, so the old string
        # named nobody on the one page where it matters most: the delete
        # confirmation.
        return f"{self.user.email} in {self.group}"


class TierPrice(models.Model):
    """One quantity break: this group pays `amount` each from `min_quantity` up.

    An absolute price, not a discount off retail, which is what makes 2.4
    enforceable: there is nothing here for a voucher to combine with.
    """

    variant = models.ForeignKey(
        "product.ProductVariant",
        related_name="wsm_tier_prices",
        on_delete=models.CASCADE,
        help_text=(
            "The exact SKU this price is for. Search by product name or SKU."
        ),
    )
    group = models.ForeignKey(
        DealerGroup,
        related_name="tier_prices",
        on_delete=models.CASCADE,
        help_text="The buyer group that pays this price.",
    )
    min_quantity = models.PositiveIntegerField(
        default=1,
        help_text=(
            "This price applies from this quantity up, until the next break. "
            "Use 1 for the everyday price of this group."
        ),
    )
    # Matched to CheckoutLine.price_override so the number round-trips onto the
    # line without a quantize step that could move a cent.
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        help_text=(
            "What this group pays EACH, in the shop's currency. A price, not a "
            "discount: no voucher or promotion is added on top unless discount "
            "stacking is turned on in Dealer settings."
        ),
    )

    class Meta:
        ordering = ("variant_id", "group_id", "min_quantity")
        constraints = [
            models.UniqueConstraint(
                fields=["variant", "group", "min_quantity"],
                name="wsm_dealer_one_row_per_break",
            )
        ]

    def __str__(self):
        """What this row IS, on the pages Django titles with it.

        The old string was "7 x1: 228.000": a DealerGroup row id the merchant has
        never seen, no product, no SKU. That is the heading of the change page,
        the breadcrumb, the history and, worst, the delete confirmation.

        Two joins, on the three pages that call this and none on the changelist,
        which lists explicit columns. The currency is deliberately not looked up
        here: it would be a third query on every row of a delete confirmation,
        and the form's own label carries it where a merchant is typing.
        """
        variant = self.variant
        sku = variant.sku or variant.product.name
        return f"{sku} - {self.group} at qty {self.min_quantity}: {self.amount:.2f}"


class DealerSettings(models.Model):
    """Per-instance toggles. One row, or none.

    No row means the defaults, so a fresh install is default-deny without a data
    migration: `discount_stacking` off is requirement 2.4's ruling. The admin
    creates the row on first visit so a merchant can SEE what the default is,
    which an empty list never told anyone.
    """

    discount_stacking = models.BooleanField(
        default=False,
        help_text=(
            "Off (the default): a line already at a dealer price takes no "
            "voucher, promotion or order-level discount on top. On: discounts "
            "combine with dealer prices."
        ),
    )

    class Meta:
        verbose_name_plural = "dealer settings"

    def __str__(self):
        return "Dealer settings"

    @classmethod
    def stacking_enabled(cls) -> bool:
        row = cls.objects.first()
        return bool(row and row.discount_stacking)
