# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Compose's own tables. Core Saleor tables are never altered; FKs into them are.

Every table here is created by this app's migrations under the `wsm_compose`
app label, which is where the `wsm_compose_` name prefix comes from: Django's
default table naming already carries it, so no model spells out a db_table.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from .. import money
from . import pricing

# What a merchant reads for each stored value. The value itself is the
# storefront's contract and never moves; only the wording does. Kept derived
# from pricing so a type added there still appears, under its raw name until
# someone words it.
PROMPT_LABELS = {
    pricing.CHOICE_ONE: "Pick one",
    pricing.CHOICE_MANY: "Pick any number",
    "text": "Type an answer",
    "date": "Pick a date",
    "datetime": "Pick a date and time",
    "image": "Upload an image",
}
BASIS_LABELS = {
    pricing.FIXED: "A flat amount of money",
    pricing.PERCENT: "A percentage of the item price",
}
SCOPE_LABELS = {
    pricing.PER_UNIT: "Per item",
    pricing.PER_LINE: "Once per line",
}
PROMPT_TYPE_CHOICES = [(p, PROMPT_LABELS.get(p, p)) for p in pricing.PROMPT_TYPES]
FEE_BASIS_CHOICES = [(b, BASIS_LABELS.get(b, b)) for b in pricing.FEE_BASES]
FEE_SCOPE_CHOICES = [(s, SCOPE_LABELS.get(s, s)) for s in pricing.FEE_SCOPES]

# One rounding rule for the whole fork, HALF_UP, lives in wsm/money.py. Kept as
# a name here because this module is where every writer already imports it from.
to_cents = money.to_cents


def tier_group_choices():
    """Every dealer group code, for the admin dropdown and the error message.

    ponytail: wsm.dealer owns this namespace (see its models docstring) and is
    growing its own `validate_tier_group_code`. Compose asks the database
    directly until that lands; the upgrade path is to delete these two functions
    and import that one, and nothing else here changes.
    """
    from ..dealer.models import DealerGroup

    return list(DealerGroup.objects.values_list("code", flat=True))


def validate_tier_group_code(code: str) -> None:
    """A tier group naming no dealer group prices nothing: a silent no-op row."""
    from ..dealer.models import DealerGroup

    if not code:
        return
    if DealerGroup.objects.filter(code=code).exists():
        return
    known = ", ".join(tier_group_choices()) or "none yet"
    raise ValidationError(
        f"There is no dealer group called {code!r}, so this price would never be "
        f"charged to anyone. Dealer groups that exist: {known}."
    )


def cheapest_listed_cents(product_id) -> int | None:
    """The lowest price any of this product's variants is listed at, in cents.

    The base a configured price starts from, taken at its lowest so the floor
    below is the worst case across every variant and channel. None when the
    product has no priced listing at all: there is no configured price to check
    yet, and refusing an edit because the catalog is half built would be its own
    defect.
    """
    from ...product.models import ProductVariantChannelListing

    amount = (
        ProductVariantChannelListing.objects.filter(
            variant__product_id=product_id, price_amount__isnull=False
        )
        .order_by("price_amount")
        .values_list("price_amount", flat=True)
        .first()
    )
    return None if amount is None else to_cents(amount)


def configured_floor_cents(
    product_id,
    *,
    pending_set=None,
    pending_values=(),
    removed_value_pks=(),
    tier_group=None,
    pending_tiers=(),
):
    """The cheapest configuration this product can produce, counting unsaved edits.

    Returns (floor_cents, base_cents), or (None, None) when the product has no
    priced listing. `pending_set` and `pending_values` are the rows the merchant
    is about to save and `removed_value_pks` the ones they are deleting: each
    replaces its stored self, so the check is on the catalog as it WOULD be
    rather than as it is. That is what makes the rule hold for a whole inline
    formset of new credits, not just for one row at a time.

    `tier_group` prices every value the way `pricing.delta_for` would price it
    for that buyer group, better of the tier row and retail, so the floor is the
    DEALER's floor. Without it the answer is retail's. A dealer floor is its own
    number because a tier credit can be deeper than the retail one, and the
    product whose retail floor is a dollar can still hand one buyer group a
    configuration that comes to nothing. `pending_tiers` are the tier rows being
    saved, each replacing its stored self, same as `pending_values`.

    Reads the whole product's sets: one query plus one prefetch per save, plus a
    second prefetch when a tier group is asked about. That is the price of a
    cross-row invariant, and an admin save is not a hot path.
    """
    base = cheapest_listed_cents(product_id)
    if base is None:
        return None, None

    pending_pks = {v.pk for v in pending_values if v.pk}
    dropped = set(removed_value_pks) | pending_pks

    prefetch = ["values"]
    if tier_group:
        prefetch.append("values__tier_deltas")

    priced_sets = []
    for stored in OptionSet.objects.filter(product_id=product_id).prefetch_related(
        *prefetch
    ):
        prompt_type, required = stored.prompt_type, stored.required
        if pending_set is not None and pending_set.pk == stored.pk:
            prompt_type, required = pending_set.prompt_type, pending_set.required
        values = [
            _floor_value(v, tier_group, pending_tiers)
            for v in stored.values.all()
            if v.pk not in dropped
        ]
        values += [
            _floor_value(v, tier_group, pending_tiers)
            for v in pending_values
            if v.option_set_id == stored.pk
        ]
        priced_sets.append(
            pricing.OptionSet(
                id=stored.pk,
                prompt_type=prompt_type,
                required=required,
                values=tuple(values),
            )
        )

    # A set being CREATED is not in the loop above and its values carry no
    # option_set_id yet, so the first save of a question full of credits would
    # otherwise skip the rule entirely.
    if pending_set is not None and pending_set.pk is None:
        priced_sets.append(
            pricing.OptionSet(
                id=0,
                prompt_type=pending_set.prompt_type,
                required=pending_set.required,
                values=tuple(
                    _floor_value(v, tier_group, pending_tiers)
                    for v in pending_values
                    if v.option_set_id is None
                ),
            )
        )

    return pricing.minimum_configured_cents(base, priced_sets), base


def _floor_value(value, tier_group=None, pending_tiers=()) -> pricing.Value:
    """One OptionValue row, saved or not, as the pricing engine reads it."""
    delta = to_cents(value.price_delta or 0)
    if tier_group:
        tier = _tier_delta(value, tier_group, pending_tiers)
        if tier is not None:
            # Better of, which is what `pricing.delta_for` charges.
            delta = min(to_cents(tier), delta)
    return pricing.Value(
        id=value.pk or 0,
        name=value.name,
        sku_fragment=value.sku_fragment,
        price_delta=delta,
        sort_order=value.sort_order or 0,
    )


def _tier_delta(value, tier_group, pending_tiers):
    """This group's delta for this value: the row being saved, or the stored one."""
    for row in pending_tiers:
        if row.tier_group == tier_group and row.option_value_id == value.pk:
            return row.price_delta
    if value.pk is None:
        return None
    # Prefetched by `configured_floor_cents` for every value it loaded itself.
    for row in value.tier_deltas.all():
        if row.tier_group == tier_group:
            return row.price_delta
    return None


def tier_groups_on(product_id) -> list[str]:
    """Every dealer group with a delta anywhere on this product. One query."""
    return list(
        DealerTierOptionPrice.objects.filter(
            option_value__option_set__product_id=product_id
        )
        .values_list("tier_group", flat=True)
        .distinct()
    )


def dealer_floor_problem(product_id, **pending) -> ValidationError | None:
    """The first dealer group this edit would price down to nothing, if any.

    Checked from every screen that can move the floor, because the retail check
    passing says nothing about a group whose credits are deeper: the retail
    floor is an upper bound on every dealer floor, never the same number.
    Costs one query, plus two per group that actually has rows on this product,
    and nothing at all on the products that have none.
    """
    for code in tier_groups_on(product_id):
        floor, base = configured_floor_cents(product_id, tier_group=code, **pending)
        if floor is not None and floor <= 0:
            return dealer_floor_error(code, floor, base)
    return None


def floor_error(floor_cents: int, base_cents: int) -> ValidationError:
    """One sentence a merchant can act on, in currency units, never in cents."""
    return ValidationError(
        f"Saving this would let a shopper configure this product all the way down "
        f"to {pricing.format_money(floor_cents)}, and a configured price has to "
        f"stay above zero. The product's cheapest listed price is "
        f"{pricing.format_money(base_cents)} and the credits on it now add up to "
        f"more than that. Reduce the credit until the configured total stays "
        f"above zero, or raise the product's price."
    )


def dealer_floor_error(
    tier_group: str, floor_cents: int, base_cents: int
) -> ValidationError:
    """The floor sentence, said about one buyer group instead of about retail."""
    return ValidationError(
        f"Saving this would let a {tier_group} dealer configure this product all "
        f"the way down to {pricing.format_money(floor_cents)}, and a configured "
        f"price has to stay above zero for every buyer. The product's cheapest "
        f"listed price is {pricing.format_money(base_cents)} and this group pays "
        f"the better of its own dealer price and retail on every choice, so its "
        f"credits add up to more than that. Reduce the dealer credit, or raise "
        f"the product's price."
    )


def duplicate_fragment_error(other_name: str, fragment: str) -> ValidationError:
    return ValidationError(
        f"{other_name!r} in this question already uses the SKU code {fragment!r}. "
        f"Two choices with the same code produce the same SKU on the order, so "
        f"nobody can tell which one was bought. Give this one its own code, or "
        f"leave it blank."
    )


class OptionSet(models.Model):
    """One question a product asks, and the answers it accepts."""

    # Set by the admin form when the value inline is on screen and will check
    # the floor across every row at once. A single-row check there compares a
    # row being fixed against siblings still stored broken, and refuses the very
    # submit that fixes them.
    floor_checked_by_formset = False

    product = models.ForeignKey(
        "product.Product",
        related_name="wsm_option_sets",
        on_delete=models.CASCADE,
        help_text="The product that asks this question.",
    )
    name = models.CharField(
        max_length=250,
        help_text=(
            "Your internal name for this question. Shown to shoppers only when "
            "Label is empty."
        ),
    )
    label = models.CharField(
        max_length=250,
        blank=True,
        help_text="What the shopper sees above the choices, e.g. Choose a finish.",
    )
    prompt_type = models.CharField(
        max_length=20,
        choices=PROMPT_TYPE_CHOICES,
        default=pricing.CHOICE_ONE,
        help_text=(
            "Pick one and Pick any number are answered from the choices below. "
            "The rest ask the shopper to type or upload something instead, and "
            "add no money."
        ),
    )
    required = models.BooleanField(
        default=False,
        help_text="The shopper cannot add this product to the cart without answering.",
    )
    note = models.TextField(
        blank=True, help_text="Optional help shown to the shopper under this question."
    )
    sort_order = models.IntegerField(
        default=0, help_text="Low numbers first. Ties fall back to the order created."
    )

    class Meta:
        ordering = ("sort_order", "pk")

    def __str__(self):
        return self.label or self.name

    def clean(self):
        """Making a set required, or one-of, can push the floor under zero too.

        The same invariant as `OptionValue.clean()`, from the other side: a
        question full of credits priced fine while a shopper could decline it,
        and turning Required on makes the cheapest answer mandatory. Raised as a
        form-wide error because either field can be the cause.
        """
        super().clean()
        if self.floor_checked_by_formset or self.pk is None or self.product_id is None:
            return
        floor, base = configured_floor_cents(self.product_id, pending_set=self)
        if floor is not None and floor <= 0:
            raise floor_error(floor, base)
        problem = dealer_floor_problem(self.product_id, pending_set=self)
        if problem is not None:
            raise problem

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

    # See OptionSet.floor_checked_by_formset.
    floor_checked_by_formset = False

    option_set = models.ForeignKey(
        OptionSet,
        related_name="values",
        on_delete=models.CASCADE,
        help_text="The question this is an answer to.",
    )
    name = models.CharField(
        max_length=250, help_text="What the shopper sees for this choice."
    )
    sku_fragment = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Added to the product SKU when this choice is picked, so the order and "
            "the warehouse can tell configurations apart. Has to be different from "
            "every other choice in this question. Leave blank to add nothing."
        ),
    )
    price_delta = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=(
            "Added to the product price when this choice is picked. A negative "
            "amount is a credit that subtracts; 0 is free. The credits on one "
            "product can never add up to more than the product's price."
        ),
    )
    image_url = models.URLField(
        max_length=500,
        blank=True,
        help_text="Optional swatch or thumbnail shown next to this choice.",
    )
    sort_order = models.IntegerField(
        default=0, help_text="Low numbers first. Ties fall back to the order created."
    )

    class Meta:
        ordering = ("sort_order", "pk")

    def __str__(self):
        return self.name

    def clean(self):
        """The two rules that were saving a broken buy button with a success message.

        Both live here rather than on the admin form so every writer that
        validates gets them. There is no REST write path for these rows today
        (compose/views.py is read plus checkout), so the admin is the only
        caller; putting the rule on a form would have had to move the day one
        appeared.

        The duplicate-fragment rule is deliberately NOT a database constraint.
        Fuel Lab's live catalog carries eight colliding pairs (the four QSST
        surge-tank pump questions, where 49614 and 494xx each name both a dual
        and a triple pump), so a UNIQUE index would refuse the 5.0 import that
        produced them. ponytail: the ceiling is that a bulk writer can still
        create a collision; the upgrade path is to correct those sixteen source
        rows, then add UniqueConstraint(option_set, sku_fragment) under a
        condition of ~Q(sku_fragment="") in a migration.
        """
        super().clean()
        errors = {}

        if (
            self.sku_fragment
            and self.option_set_id
            and not self.floor_checked_by_formset
        ):
            clash = (
                OptionValue.objects.filter(
                    option_set_id=self.option_set_id, sku_fragment=self.sku_fragment
                )
                .exclude(pk=self.pk)
                .first()
            )
            if clash is not None:
                errors["sku_fragment"] = duplicate_fragment_error(
                    clash.name, self.sku_fragment
                )

        if self.option_set_id and not self.floor_checked_by_formset:
            product_id = self.option_set.product_id
            floor, base = configured_floor_cents(product_id, pending_values=[self])
            if floor is not None and floor <= 0:
                errors["price_delta"] = floor_error(floor, base)
            else:
                # A retail credit this product can afford can still take one
                # buyer group under, because that group pays the better of its
                # own delta and this one on every choice.
                problem = dealer_floor_problem(product_id, pending_values=[self])
                if problem is not None:
                    errors["price_delta"] = problem

        if errors:
            raise ValidationError(errors)

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
        OptionValue,
        related_name="tier_deltas",
        on_delete=models.CASCADE,
        help_text="The choice this group is being priced on.",
    )
    tier_group = models.CharField(
        max_length=100,
        help_text=(
            "The dealer group this price is for. It has to name a group that "
            "exists, or nobody is ever charged it."
        ),
    )
    price_delta = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text=(
            "What this group pays for the choice instead of the retail amount. "
            "Never more than the retail amount; a credit is taken as written."
        ),
    )

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

    def clean(self):
        """Three ways this row saved clean and then priced something else.

        Free text named a group nobody belongs to. An amount above retail saved,
        quoted retail on the product page and then refused the add-to-cart with
        a message written for a developer, because the ceiling lived two layers
        away in `pricing.delta_for`. And a credit deeper than the product could
        carry took that group's cheapest configuration to nothing, which no
        retail-side floor check could see. All three are field errors here now,
        at the one moment a merchant can still fix them.
        """
        super().clean()
        errors = {}

        try:
            validate_tier_group_code(self.tier_group)
        except ValidationError as invalid:
            errors["tier_group"] = invalid

        if self.option_value_id and self.price_delta is not None:
            retail = to_cents(self.option_value.price_delta or 0)
            # The ceiling is the retail delta floored at zero: a credit stands
            # as written, but a value retail gives away is never a dealer
            # surcharge. Same test `pricing.delta_for` applies when it charges.
            ceiling = max(retail, 0)
            if to_cents(self.price_delta) > ceiling:
                errors["price_delta"] = ValidationError(
                    f"Retail pays {pricing.format_money(retail)} for this choice, "
                    f"so a {self.tier_group} dealer cannot be charged "
                    f"{pricing.format_money(to_cents(self.price_delta))} for it. A "
                    f"dealer price is never above retail: enter the retail amount "
                    f"or less, or a negative amount for a credit."
                )

        if not errors and self.option_value_id and self.price_delta is not None:
            floor, base = configured_floor_cents(
                self.option_value.option_set.product_id,
                tier_group=self.tier_group,
                pending_tiers=[self],
            )
            if floor is not None and floor <= 0:
                errors["price_delta"] = dealer_floor_error(
                    self.tier_group, floor, base
                )

        if errors:
            raise ValidationError(errors)

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
        "product.Product",
        related_name="wsm_fees",
        on_delete=models.CASCADE,
        help_text="The product this charge is attached to.",
    )
    label = models.CharField(
        max_length=250,
        help_text="What the shopper sees on the cart line, e.g. Freight crating.",
    )
    sku = models.CharField(
        max_length=100,
        blank=True,
        help_text="Your own code for this charge, carried through to the order.",
    )
    basis = models.CharField(
        max_length=10,
        choices=FEE_BASIS_CHOICES,
        default=pricing.FIXED,
        help_text=(
            "A flat amount of money, or a percentage of the configured product "
            "price before any other charge."
        ),
    )
    # Money when basis is fixed, a percentage when it is percent.
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text=(
            "A flat charge, or the percentage when Charged as is a percentage "
            "(enter 8.25 for 8.25%). Never negative, and never above 100 for a "
            "percentage."
        ),
    )
    apply_to = models.CharField(
        max_length=10,
        choices=FEE_SCOPE_CHOICES,
        default=pricing.PER_UNIT,
        help_text=(
            "Per item charges once for every item ordered. Once per line charges "
            "once no matter how many are ordered."
        ),
    )
    required = models.BooleanField(
        default=True,
        help_text=(
            "Always charged. Turn this off to let the shopper decline the charge."
        ),
    )
    decline_label = models.CharField(
        max_length=250,
        blank=True,
        help_text=(
            "Only used when the charge can be declined: the wording of the decline "
            "option, e.g. No crate, I will collect."
        ),
    )
    # The hidden variant this fee's checkout line points at (U2). Fee is our
    # table, so the column is ours; the FK reaches into a core table, which the
    # design allows, and no core table is altered.
    variant = models.ForeignKey(
        "product.ProductVariant",
        related_name="+",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=(
            "Internal. The hidden catalog row this charge rides on the order as, "
            "created automatically the first time the charge is bought."
        ),
    )

    class Meta:
        ordering = ("pk",)

    def __str__(self):
        return self.label

    def clean(self):
        """A charge is money the shopper OWES: never negative, never over 100%.

        A negative charge is the one fee shape that can carry a whole line to
        zero or below and 422 at add-to-cart, and there is no merchant reason to
        want one: a reduction belongs on an option choice as a credit, where the
        floor rule already guards it. A percentage above 100 more than doubles
        the price and is a decimal-point slip every time. No override flag: a
        rule with an escape hatch nobody asked for is not a rule.
        """
        super().clean()
        if self.amount is None:
            return
        if self.amount < 0:
            raise ValidationError(
                {
                    "amount": ValidationError(
                        "A charge cannot be negative. To reduce a price, add an "
                        "option choice with a credit instead."
                    )
                }
            )
        if self.basis == pricing.PERCENT and self.amount > 100:
            raise ValidationError(
                {
                    "amount": ValidationError(
                        "A percentage charge cannot be more than 100%. Enter 8.25 "
                        "for 8.25%."
                    )
                }
            )

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
        # `wsm-fee` is the exclusion key. Fee products are catalog plumbing
        # with a real, published URL (the checkout write path refuses an
        # unpublished variant), so the storefront, the sitemap, the feed and
        # the PartsLogic indexer exclude them by product type rather than by a
        # slug prefix or a new field. Named in BAKEOFF-design section 8.
        # Never shipping required: a Fee carries no freight marker of its own
        # (`freight_class` lives on KitConfig, on the kit, not on the charge),
        # so the charge never asks the shipping engine for a rate of its own.
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
    # Both amounts, always. Stock `get_variant_availability` guards a NULL
    # `price` and then dereferences `discounted_price` unguarded, so a listing
    # carrying only the first of the two turns the public
    # `variants { pricing }` field into a 500 for anyone, with no login, on
    # every fee a merchant has ever sold. The sibling product listing above
    # already writes both; this row was the omission.
    listing, created = ProductVariantChannelListing.objects.get_or_create(
        variant=variant,
        channel=channel,
        defaults={
            "currency": channel.currency_code,
            "price_amount": Decimal("0"),
            "discounted_price_amount": Decimal("0"),
        },
    )
    # The rows minted before that line existed repair themselves the next time
    # this runs. A one-off management command would be a second writer of the
    # same column for a set that is four rows wide today; this path already
    # holds the row, so a correct listing costs nothing and a broken one costs
    # one UPDATE, once.
    if not created and listing.discounted_price_amount is None:
        listing.discounted_price_amount = listing.price_amount or Decimal("0")
        listing.save(update_fields=["discounted_price_amount"])
    _ENSURED_FEE_VARIANTS.add((fee.pk, channel.pk))
    return variant
