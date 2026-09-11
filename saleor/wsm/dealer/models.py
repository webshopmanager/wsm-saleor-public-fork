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

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


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


# The smallest amount that is still a price once it is rounded to the cent it
# will be charged at. Named because the field validator, the table constraint
# and the pricing floor are three statements of the same rule.
MIN_TIER_AMOUNT = Decimal("0.01")


class TierPrice(models.Model):
    """One quantity break: this group pays `amount` each from `min_quantity` up.

    An absolute price, not a discount off retail, which is what makes 2.4
    enforceable: there is nothing here for a voucher to combine with.
    """

    variant = models.ForeignKey(
        "product.ProductVariant",
        related_name="wsm_tier_prices",
        on_delete=models.CASCADE,
        help_text=("The exact SKU this price is for. Search by product name or SKU."),
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
    #
    # A tier price is an absolute price, so a zero or negative one is not a big
    # discount, it is a line that pays the shopper. The floor is ONE CENT rather
    # than "above zero" because the amount carries three decimals and is charged
    # at two: 0.004 is a positive number that charges 0.00.
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(MIN_TIER_AMOUNT)],
        help_text=(
            "What this group pays EACH, in the shop's currency, and at least one "
            "cent. A price, not a discount: no voucher or promotion is added on "
            "top unless discount stacking is turned on in Dealer settings."
        ),
    )

    class Meta:
        ordering = ("variant_id", "group_id", "min_quantity")
        constraints = [
            models.UniqueConstraint(
                fields=["variant", "group", "min_quantity"],
                name="wsm_dealer_one_row_per_break",
            ),
            # The backstop under the field validator, for the writers that never
            # call full_clean(): the 5.0 importer, a shell, a SQL fixup. Live on
            # the bake-off box an amount of -50 reached a checkout line and
            # quoted a unit price of -50.00.
            models.CheckConstraint(
                condition=Q(amount__gte=MIN_TIER_AMOUNT),
                name="wsm_dealer_tier_amount_at_least_a_cent",
            ),
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
        constraints = [
            # The row IS the store, so there is exactly one and its key is
            # known. Without this, two concurrent saves of the settings screen
            # each insert a row and the reader picks one of them.
            models.CheckConstraint(
                condition=Q(pk=1),
                name="wsm_dealer_settings_is_one_row",
            ),
        ]

    def __str__(self):
        return "Dealer settings"

    def save(self, *args, **kwargs):
        """The row IS the store, so it is always the same row.

        Without this, `DealerSettings.objects.create(...)` takes the next value
        of the sequence and the CheckConstraint refuses it: correct, and a trap
        for every caller that just wants "the settings". Pinning the key here
        means a writer cannot MISS the singleton, and the constraint under it
        goes back to being what the tier-amount one is: the backstop for the
        writers that never come through Django at all.
        """
        self.pk = 1
        return super().save(*args, **kwargs)

    @classmethod
    def stacking_enabled(cls) -> bool:
        """The one row, named by key rather than by whichever came back first.

        `.first()` on an unordered queryset already orders by pk, so this was
        deterministic; saying so costs nothing and survives someone giving this
        model a `Meta.ordering` later, which would silently change WHICH row
        decides whether a discount stacks on a dealer price.
        """
        row = cls.objects.order_by("pk").first()
        return bool(row and row.discount_stacking)
