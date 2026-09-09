# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Compose's own tables. Core Saleor tables are never altered; FKs into them are.

Every table here is created by this app's migrations under the `wsm_compose`
app label, which is where the `wsm_compose_` name prefix comes from: Django's
default table naming already carries it, so no model spells out a db_table.
"""

import json
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Min
from django.db.models.signals import post_delete, post_save

from .. import money
from . import pricing

logger = logging.getLogger(__name__)

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
        # A question means nothing without the product it is asked on, and this
        # is the string on the delete confirmation and in every picker.
        return f"{self.product.name}: {self.label or self.name}"

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
        # "Black" alone does not say which question, on which product, is about
        # to lose a choice.
        return f"{self.name} ({self.option_set})"

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


# The product metafield the PDP configurator gates on: the storefront asks a
# product nothing without it (design doc, section 2). The 5.0 importer has its
# own copy of these two strings; `test_the_importer_and_the_admin_write_the_same_marker`
# is what keeps the two from drifting apart.
CONFIGURABLE_METAFIELD = "compose.configurable"
CONFIGURABLE_VALUE = "true"

# The lowest price a shopper can actually pay for this product, per channel.
# Public, derived, and never read on a shopper path: see the design doc,
# "Price floor stamp".
PRICE_FLOOR_METAFIELD = "wsm.price_floor"

# Prop 65 rides the same public-stamp lane, under the key names the interim
# Compose service published (`external_apps/compose/rules/metadata.py`), so a
# consumer written against that service reads this one without a change. The
# warning TYPE is the key rather than a value, which is what keeps the contract
# closed: a second kind of warning gets a second key, and until one exists no
# reader has to parse anything.
PROP65_METAFIELD = "pl.rules.prop65"
PROP65_TEXT_METAFIELD = "pl.rules.prop65.text"
PROP65_VALUE = "true"


class ProductCompliance(models.Model):
    """What a product must SAY, and where it may not GO. One row per product.

    Dana's ruling (2026-09-09): every tenant needs Prop 65, because California
    law is not a feature, and shipping restrictions are the same class of fact
    (a CARB part that may not be sold into California). One table because they
    are one merchant decision, made in one place, on one screen: a product
    carries at most one disclosure and at most one destination rule, and the
    several-states case is the `restricted_states` list, not several rows.

    Nothing here is money, so nothing here is on a pricing path. The disclosure
    is denormalized onto the product at WRITE time (`sync_product_stamps`) the
    way the floor is; the destination rule is read once, at order creation, by
    `restrictions.is_destination_serviced`.
    """

    product = models.OneToOneField(
        "product.Product",
        related_name="wsm_compliance",
        on_delete=models.CASCADE,
        help_text="The product this warning and these restrictions belong to.",
    )
    prop65 = models.BooleanField(
        default=False,
        help_text=(
            "Show the California Proposition 65 warning on this product. The "
            "storefront draws the standard short-form warning, pictogram "
            "included, unless you write your own wording below."
        ),
    )
    prop65_text = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Optional. Your own Prop 65 wording, e.g. the exact WARNING "
            "sentence the supplier gives you. Empty means the standard one."
        ),
    )
    restricted_states = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "US state codes this product cannot ship to, comma separated, e.g. "
            "CA for a part that is not CARB legal. Empty restricts nothing."
        ),
    )
    include_shipping_zones = models.ManyToManyField(
        "shipping.ShippingZone",
        blank=True,
        related_name="wsm_compliance_rows",
        help_text=(
            "Optional. Naming zones here means this product ships ONLY to the "
            "countries those zones cover. Empty means every destination is "
            "serviced, which is what almost every product wants."
        ),
    )
    restriction_message = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "What the shopper reads when their destination is refused. Empty "
            "means a standard sentence naming the item and the destination."
        ),
    )

    class Meta:
        verbose_name = "product compliance"
        verbose_name_plural = "product compliance"

    def __str__(self):
        return f"{self.product.name}: compliance"

    @property
    def state_codes(self) -> tuple[str, ...]:
        """The stored codes, normalised, for the matcher to compare against."""
        return tuple(
            code.strip().upper()
            for code in (self.restricted_states or "").split(",")
            if code.strip()
        )

    def refusal_message(self, where: str) -> str:
        """The sentence the shopper reads. The merchant's wording wins."""
        if self.restriction_message.strip():
            return self.restriction_message.strip()
        return f"{self.product.name} cannot be shipped to {where}."

    def clean(self):
        """A state code nothing can match is a restriction that does nothing.

        Checked here, on the screen the typo was made on, rather than at
        checkout: a merchant who writes CAL for California gets told so while
        they are looking at the field, and never finds out from an order that
        should have been refused and was not.
        """
        from .restrictions import US_SUBDIVISIONS

        codes = self.state_codes
        unknown = [code for code in codes if code not in US_SUBDIVISIONS]
        if unknown:
            raise ValidationError(
                {
                    "restricted_states": (
                        "Not US state codes: "
                        + ", ".join(unknown)
                        + ". Use two-letter codes, e.g. CA, HI, AK."
                    )
                }
            )
        self.restricted_states = ", ".join(codes)


def _channel_bases(product_id):
    """Every channel this product is priced in, with its cheapest listed price.

    One query, grouped in the database. Per channel because a base price is per
    channel: a product listed in four channels has four floors, and a "from"
    price quoted in the wrong currency is worse than no price at all. Cheapest
    across the product's variants for the same reason `cheapest_listed_cents`
    takes the lowest: a floor is the worst case, and a configurable product has
    one variant anyway (Dana, 2026-09-05, an option value is never a variant).

    A channel the product is not priced in is simply absent, so a half-built
    catalog stamps nothing for it rather than stamping a zero.
    """
    from ...product.models import ProductVariantChannelListing

    rows = (
        ProductVariantChannelListing.objects.filter(
            variant__product_id=product_id, price_amount__isnull=False
        )
        .values("channel__slug", "channel__currency_code")
        .annotate(base=Min("price_amount"))
    )
    return {
        row["channel__slug"]: (to_cents(row["base"]), row["channel__currency_code"])
        for row in rows
    }


def _floor_option_sets(product_id):
    """This product's questions as the pricing engine reads them, at RETAIL.

    Two queries. Deliberately not `OptionSet.to_pricing()`, which reads each
    value's tier rows: a retail floor never looks at one, and reading them would
    buy a query per value for numbers this answer throws away. Dealer tiers are
    private to the dealer path and are never in a public stamp.
    """
    return [
        pricing.OptionSet(
            id=option_set.pk,
            prompt_type=option_set.prompt_type,
            required=option_set.required,
            values=tuple(_floor_value(v) for v in option_set.values.all()),
        )
        for option_set in OptionSet.objects.filter(
            product_id=product_id
        ).prefetch_related("values")
    ]


def price_floor_by_channel(product_id, *, option_sets=None, fees=None) -> dict:
    """The lowest price a shopper can actually pay, per channel slug.

    Empty when there is nothing worth stamping: no priced listing anywhere, or
    no configuration at all. A product carrying only DECLINABLE charges is in
    that second case on purpose, because its floor is its base price and the
    base price is already on the listing: a stamp that repeats it is a second
    copy of a number to go stale.

    Amounts are two-decimal strings in the channel's own currency, computed in
    integer cents through `pricing.minimum_line_cents` so they round exactly the
    way checkout rounds. Never a float: 493.24 as a float is 493.2399999999998,
    and a feed reading this is quoting money.
    """
    if option_sets is None:
        option_sets = _floor_option_sets(product_id)
    if fees is None:
        fees = [f.to_pricing() for f in Fee.objects.filter(product_id=product_id)]
    if not option_sets and not any(f.required for f in fees):
        return {}

    floor = {}
    for slug, (base_cents, currency) in _channel_bases(product_id).items():
        cents = pricing.minimum_line_cents(base_cents, option_sets, fees)
        if cents < 0:
            # The admin refuses a save that does this and `pricing.delta_for`
            # refuses to charge it, so reaching here means rows written around
            # both. Stamped at zero rather than negative, because a negative
            # "from" price is a feed rejection and a broken PLP, and said out
            # loud so the product can be found.
            logger.warning(
                "wsm.price_floor: product %s in channel %s floors at %s cents, "
                "which is below zero; stamping 0.00. Its required credits are "
                "deeper than its base price.",
                product_id,
                slug,
                cents,
            )
            cents = 0
        floor[slug] = {
            "amount": str(Decimal(cents).scaleb(-2)),
            "currency": currency,
        }
    return floor


def sync_product_stamps(product_id, product=None) -> set:
    """Make this product's three public stamps equal to what its rows say.

    `compose.configurable` gates the PDP configurator: the storefront asks a
    product nothing without it, so a merchant who built a question or a charge
    in /admin/ used to get a working set, a working price and a PDP that asked
    nothing. `wsm.price_floor` is the lowest price a shopper can actually pay,
    which a $0 base configurable product has no other way to report: Google
    Merchant Center suspends a feed on a zero price, and a PLP tile with no
    number is not a tile anyone clicks.

    `pl.rules.prop65` (and its text) is the California disclosure, on the same
    lane for the same reason: it is a fact about the product that a storefront
    has to draw, and computing it on a shopper read would be a join per PDP for
    an answer that only changes when a merchant saves.

    ONE function and ONE write for all three, because every door that can move
    one can move another, and three hooks on the same save paths would be three
    reads and three UPDATEs of one column.

    Index-time denormalization, Dana's standing rule: the floor is computed here
    on a merchant save and stamped, never computed on a shopper read. Nothing on
    any read, pricing or checkout path calls this.

    Returns the metadata keys that changed, which is what the importer reports
    and what makes a no-op run provably free of writes.
    """
    if not product_id:
        return set()
    from ...product.models import Product

    if product is None:
        product = Product.objects.filter(pk=product_id).only("id", "metadata").first()
    if product is None:
        # A cascading product delete takes its sets with it, and the row is
        # already gone by the time this runs.
        return set()

    option_sets = _floor_option_sets(product_id)
    fees = [f.to_pricing() for f in Fee.objects.filter(product_id=product_id)]
    changed = set()

    # Fees count as configuration on their own. A product whose only Compose row
    # is a declinable crating charge still has something the PDP has to put in
    # front of the shopper.
    configurable = bool(option_sets or fees)
    if configurable != (
        product.metadata.get(CONFIGURABLE_METAFIELD) == CONFIGURABLE_VALUE
    ):
        if configurable:
            product.metadata[CONFIGURABLE_METAFIELD] = CONFIGURABLE_VALUE
        else:
            product.metadata.pop(CONFIGURABLE_METAFIELD, None)
        changed.add(CONFIGURABLE_METAFIELD)

    # A JSON STRING, not a dict, because that is what a metadata value is:
    # GraphQL types `MetadataItem.value` as String, so a dict reaches every
    # consumer as a Python repr with single quotes, which no JSON parser reads.
    # `wsm.series` on a Collection is stamped the same way for the same reason.
    floor = price_floor_by_channel(product_id, option_sets=option_sets, fees=fees)
    blob = json.dumps(floor, sort_keys=True) if floor else None
    if blob != product.metadata.get(PRICE_FLOOR_METAFIELD):
        if blob is None:
            # Absent, never empty: a reader takes a missing key as "no floor",
            # where an empty object is a floor that answers nothing.
            product.metadata.pop(PRICE_FLOOR_METAFIELD, None)
        else:
            product.metadata[PRICE_FLOOR_METAFIELD] = blob
        changed.add(PRICE_FLOOR_METAFIELD)

    # The disclosure. One indexed read on a unique column, and the row is
    # absent for almost every product. Text is stamped only when the merchant
    # wrote their own: an absent text key with the flag present means "draw the
    # standard short-form warning", which is the interim service's contract and
    # the reason the flag alone is enough for a storefront.
    compliance = (
        ProductCompliance.objects.filter(product_id=product_id)
        .only("prop65", "prop65_text")
        .first()
    )
    wanted_prop65 = {}
    if compliance is not None and compliance.prop65:
        wanted_prop65[PROP65_METAFIELD] = PROP65_VALUE
        if compliance.prop65_text.strip():
            wanted_prop65[PROP65_TEXT_METAFIELD] = compliance.prop65_text.strip()
    for key in (PROP65_METAFIELD, PROP65_TEXT_METAFIELD):
        if wanted_prop65.get(key) == product.metadata.get(key):
            continue
        if key in wanted_prop65:
            product.metadata[key] = wanted_prop65[key]
        else:
            # Absent, never blank: a reader takes a missing key as "no warning".
            product.metadata.pop(key, None)
        changed.add(key)

    if changed:
        product.save(update_fields=["metadata"])
    return changed


def _sync_stamps_from(sender, instance, **kwargs):
    sync_product_stamps(instance.product_id)


def _sync_stamps_from_value(sender, instance, **kwargs):
    """An option value moves the floor without touching the marker.

    Its product is one join away, and on a cascading delete of the question the
    row it points at may already be gone, so this asks for the id and takes
    nothing when the answer is nothing.
    """
    product_id = (
        OptionSet.objects.filter(pk=instance.option_set_id)
        .values_list("product_id", flat=True)
        .first()
    )
    sync_product_stamps(product_id)


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
        # Charges repeat across products ("Freight crating" is on hundreds), so
        # the label alone cannot identify the row being deleted.
        return f"{self.label} on {self.product.name}"

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


# Every door that adds or removes configuration, not just `save()`: the admin
# deletes through a queryset, which skips `Model.delete` and fires this.
#
# OptionValue is here for the floor alone. It cannot change whether a product is
# configurable, but the cheapest answer to a required question IS the floor, so
# a merchant who edits one price and nothing else has to leave a current stamp
# behind. The admin's value inline saves each row through `save()`, so this is
# the one hook that covers the screen a merchant actually uses.
#
# DealerTierOptionPrice is deliberately NOT here: the stamp is retail only, and
# a tier row moves no retail number.
# ProductCompliance is here for the disclosure alone: it moves no price and no
# marker, and the flag it does move is read off the product by every storefront
# that draws a Prop 65 badge.
for _sender, _receiver in (
    (OptionSet, _sync_stamps_from),
    (Fee, _sync_stamps_from),
    (OptionValue, _sync_stamps_from_value),
    (ProductCompliance, _sync_stamps_from),
):
    post_save.connect(
        _receiver,
        sender=_sender,
        dispatch_uid=f"wsm_compose.product_stamps.save.{_sender.__name__}",
    )
    post_delete.connect(
        _receiver,
        sender=_sender,
        dispatch_uid=f"wsm_compose.product_stamps.delete.{_sender.__name__}",
    )
