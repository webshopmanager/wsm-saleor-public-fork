# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Containers' own tables. Core Saleor tables are never altered; FKs into them are.

The container IS a Saleor Collection (ruling of 2026-09-08): membership lives on
the Collection, the storefront renders the stock collection page, members are
bought without the series, and a kit is never a Saleor object beyond the
Collection. What Saleor has no column for, and only that, lives here: the axes a
series asks about, the discount a kit carries, and the quantity of each member.

Series facts reach the storefront and the search engine through the Collection's
own metadata under `wsm.series`, written by `SeriesConfig.save`. That is the
stock metadata API on a stock object, so nothing new has to be added to the
GraphQL schema for a collection page to render a series.

`SeriesConfig` is the ONE series editor (Dana, 2026-09-09). The `wsm.series`
blob is DERIVED OUTPUT of the row and nothing else: every write door restamps
it, every delete door clears it, and it is never edited by hand. A blob written
by any other hand is overwritten by the next save of the row, by design; that is
what makes the row and the blob one authority instead of two.
"""

import json

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction

from ..money import unit_amount
from . import pricing

# The raw values are the pricing module's vocabulary. A merchant reads the
# structurally identical Fee.basis as "A flat amount of money"; this field read
# "fixed", two screens away, for the same idea.
DISCOUNT_KIND_LABELS = {
    pricing.FIXED: "An amount off the whole kit",
    pricing.PERCENT: "A percentage off the whole kit",
}
DISCOUNT_KIND_CHOICES = [
    (k, DISCOUNT_KIND_LABELS.get(k, k)) for k in pricing.DISCOUNT_KINDS
]

SERIES_METADATA_KEY = "wsm.series"


# The fields the `wsm.series` blob carries. An `update()` that touches none of
# them changes nothing a reader can see, and so buys no re-stamp.
STAMPED_FIELDS = frozenset(
    {"brand", "axes", "partitioning_axis", "miss_message", "published"}
)


class SeriesConfigQuerySet(models.QuerySet):
    """Every write door leaves the Collection stamp current, not just `save()`.

    `update()`, `bulk_update()` and `bulk_create()` all skip `save()` by design,
    which is exactly why the stamp cannot live only there: a merchant publishing
    a batch from an admin list action, or an import flipping a column, writes
    through this queryset, and the readers we do not own (the storefront
    collection page, the search indexer) see only the blob. A row that says
    published under a blob that says hidden is a series that exists in the
    database and nowhere else.
    """

    def update(self, **fields):
        if not STAMPED_FIELDS.intersection(fields):
            return super().update(**fields)
        # The pks are read BEFORE the write, because the write moves rows out of
        # the filter: `filter(published=False).update(published=True)` is the
        # natural way to publish a batch, and afterwards this queryset matches
        # nothing at all.
        pks = list(self.values_list("pk", flat=True))
        rows = super().update(**fields)
        self.model._default_manager.filter(pk__in=pks).stamp()
        return rows

    def bulk_update(self, objs, fields, *args, **kwargs):
        objs = list(objs)
        rows = super().bulk_update(objs, fields, *args, **kwargs)
        if STAMPED_FIELDS.intersection(fields):
            for config in objs:
                config.stamp_collection()
        return rows

    def bulk_create(self, objs, *args, **kwargs):
        created = super().bulk_create(objs, *args, **kwargs)
        for config in created:
            config.stamp_collection()
        return created

    def delete(self, *args, **kwargs):
        """A deleted series leaves no blob behind, including the admin's bulk action.

        `delete_selected`, the only way a merchant removes more than one row,
        calls `ModelAdmin.delete_queryset` which calls this. The collections are
        read BEFORE the rows go, because afterwards there is nothing left to
        join to, and the clear rides in the same transaction as the delete so a
        failed delete cannot leave a live series with no row behind it.
        """
        collections = [config.collection for config in self.select_related("collection")]
        with transaction.atomic():
            deleted = super().delete(*args, **kwargs)
            for collection in collections:
                unstamp_collection(collection)
        return deleted

    def stamp(self):
        """One query for the rows with their collections, one write per collection."""
        for config in self.select_related("collection"):
            config.stamp_collection()


def unstamp_collection(collection):
    """Take the series off a Collection: the KEY GOES, it is not blanked.

    Absent means no series to every reader we do not own; an empty or falsy blob
    would be a series that exists and answers nothing. One write.
    """
    if SERIES_METADATA_KEY not in collection.metadata:
        return
    collection.delete_value_from_metadata(SERIES_METADATA_KEY)
    collection.save(update_fields=["metadata"])


class SeriesConfig(models.Model):
    """The ONE series editor. The Collection's `wsm.series` blob is its output.

    Ruled by Dana on 2026-09-09: this row is where a series is created, changed
    and deleted; the blob is derived and never hand-edited. So the stamp on save
    is unconditional (a stale blob can never win, whoever wrote it), and
    deleting the row clears the key rather than blanking it, because absent is
    what the storefront and the indexer read as "no series".
    """

    collection = models.OneToOneField(
        "product.Collection",
        related_name="wsm_series",
        on_delete=models.CASCADE,
        help_text=(
            "The collection whose products this series configures. Which "
            "products are in it is managed on the collection itself, in the "
            "Saleor dashboard."
        ),
    )
    # A series spans many categories and exactly ONE brand (ruled 2026-09-08).
    brand = models.CharField(
        max_length=250,
        help_text=(
            "The one brand this series covers. A series is never mixed-brand."
        ),
    )
    # Attribute slugs, in the order the configurator asks them.
    axes = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "The questions the configurator asks, in order, as product "
            "attribute slugs."
        ),
    )
    partitioning_axis = models.CharField(
        max_length=250,
        help_text=(
            "The one axis that decides WHICH product the shopper ends up on. "
            "It must be one of the axes above, and every member of the "
            "collection must carry that attribute, or publishing is refused."
        ),
    )
    miss_message = models.TextField(
        blank=True,
        help_text=(
            "What a shopper is told when their answers match nothing in this "
            "series. Leave blank for the storefront's standard wording."
        ),
    )
    published = models.BooleanField(
        default=False,
        help_text=(
            "Show this series on the storefront. Saving with this on is refused "
            "unless the collection has 2 or more published products and every "
            "one of them carries the partitioning attribute."
        ),
    )

    objects = models.Manager.from_queryset(SeriesConfigQuerySet)()

    class Meta:
        ordering = ("pk",)
        verbose_name = "series"
        verbose_name_plural = "series"

    def __str__(self):
        # A slug is the URL, not the name a merchant knows the series by.
        return f"Series: {self.collection.name}"

    def clean(self):
        """Refuse a published series that has not earned one. Unpublished is never checked.

        Three refusals, all of them scars: a one-member series is a redirect to
        the SKU wearing a series costume; a partitioning axis outside the axes
        list is a configurator that asks a question it will not use; a member
        missing the partitioning attribute is a shopper who reaches a dead end
        with no way back (5.0 series are 25 to 84 percent empty, so this is the
        common case, not the exotic one).
        """
        super().clean()
        if not self.published:
            return

        axes = list(self.axes or [])
        if self.partitioning_axis not in axes:
            raise ValidationError(
                {
                    "partitioning_axis": (
                        f"{self.partitioning_axis!r} is not one of the axes {axes!r}"
                    )
                }
            )

        members = list(
            self.collection.products.filter(channel_listings__is_published=True)
            .distinct()
            .prefetch_related("attributevalues__value__attribute")
        )
        if len(members) < 2:
            raise ValidationError(
                {
                    "published": (
                        "a series materializes only with 2 or more published "
                        f"members; this collection has {len(members)}"
                    )
                }
            )

        missing = [
            product.slug
            for product in members
            if self.partitioning_axis
            not in {
                assigned.value.attribute.slug
                for assigned in product.attributevalues.all()
            }
        ]
        if missing:
            raise ValidationError(
                {
                    "published": (
                        f"every member must carry {self.partitioning_axis!r}; "
                        f"missing on {sorted(missing)}"
                    )
                }
            )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.stamp_collection()

    def delete(self, *args, **kwargs):
        """Deleting the editor's row deletes what it published. Same transaction."""
        collection = self.collection
        with transaction.atomic():
            deleted = super().delete(*args, **kwargs)
            unstamp_collection(collection)
        return deleted

    def stamp_collection(self):
        """Publish the series facts on the Collection, for readers we do not own.

        The storefront's collection page and the search indexer both read this;
        neither gets a new GraphQL field, because Saleor already returns a
        Collection's metadata.

        Unconditional on purpose: the row is the authority, so this overwrites
        whatever the key held, and the blob is never merged with, diffed
        against, or read back.
        """
        self.collection.store_value_in_metadata(
            {
                SERIES_METADATA_KEY: json.dumps(
                    {
                        "brand": self.brand,
                        "axes": list(self.axes or []),
                        "partitioning_axis": self.partitioning_axis,
                        "miss_message": self.miss_message,
                        # Visibility rides in the blob because the stamp fires
                        # on EVERY save: presence of the key means a series
                        # exists, never that it is live. Readers default a
                        # missing `published` to hidden, so an unpublish must
                        # land here as False or a dark series stays lit.
                        "published": self.published,
                    },
                    sort_keys=True,
                )
            }
        )
        self.collection.save(update_fields=["metadata"])


class UnknownKitMember(pricing.KitRefusal):
    """A pick that is not in this kit. Refused, never quietly dropped.

    A `KitRefusal` for the same reason `KitRulesRefused` is one: every path that
    already refuses a kit it cannot charge refuses this too, and the endpoint
    that knows how to name the parts names them.
    """

    code = "kit_member_unknown"

    def __init__(self, variant_ids):
        self.variant_ids = sorted(variant_ids)
        super().__init__("this kit does not contain one of the parts chosen")


class KitConfig(models.Model):
    """What a Collection needs to behave as a kit: a discount, and members."""

    collection = models.OneToOneField(
        "product.Collection",
        related_name="wsm_kit",
        on_delete=models.CASCADE,
        help_text=(
            "The collection this kit is sold as. Its products are the kit's "
            "members, listed below with the quantity of each."
        ),
    )
    discount_kind = models.CharField(
        max_length=10,
        choices=DISCOUNT_KIND_CHOICES,
        default=pricing.FIXED,
        help_text=(
            "Whether the saving below is money off the kit or a share of "
            "what the kit's own members add up to."
        ),
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text=(
            "The saving off the members' own prices added up: an amount in the "
            "shop's currency, or a percentage, per the kind above. 0 sells the "
            "kit at the sum of its parts."
        ),
    )
    freight_class = models.CharField(
        max_length=50,
        blank=True,
        help_text=(
            "Freight class for the kit as one shipment. Blank leaves shipping "
            "to work off the members."
        ),
    )
    active = models.BooleanField(
        default=True,
        help_text="Off takes the kit price away; the members still sell on their own.",
    )

    class Meta:
        ordering = ("pk",)
        verbose_name = "kit"
        verbose_name_plural = "kits"

    def __str__(self):
        return f"Kit: {self.collection.name}"

    @property
    def discount_percent(self):
        """The percentage a merchant typed, without the trailing zeros. None if flat.

        `10.00%` is two characters of noise on a row a shopper reads, and the
        number belongs to the row rather than to the screen: the kit response
        and the admin column both take it from here.
        """
        if self.discount_kind != pricing.PERCENT:
            return None
        return f"{self.discount_amount:.2f}".rstrip("0").rstrip(".")

    @property
    def discount_label(self):
        """The saving as its own row, named the way a shopper would name it.

        Derived, not typed: the collection already carries the merchant's name
        for the bundle and the discount carries its size, so a label column
        would be a third place to keep them agreeing.
        ponytail: the ceiling is that a merchant cannot word this row
        themselves. The upgrade is one `label` field on this model, the day one
        asks.
        """
        percent = self.discount_percent
        if percent is None:
            return f"{self.collection.name} bundle discount"
        return f"{self.collection.name} bundle discount ({percent}%)"

    def pricing_members(self, channel, variant_ids=None):
        """Price this kit's members in one channel, in two queries, in member order.

        Two rather than one because a channel listing per member is a second
        table and joining it would still return a row per member: the second
        query is the cheap half of the pair, and it keeps the member rows from
        multiplying if a variant is ever listed in more than one channel.

        A member's price is what that member SELLS FOR on its own today, the
        merchant's catalogue promotion included (`money.unit_amount`), and the
        kit's own discount then comes off that. The list price was charged here
        until 2026-09-09, which overcharged every kit holding a part the merchant
        had put on sale.

        `variant_ids` is the shopper's PICKS. A kit page that offers a manual or
        an HD tensioner sends back the one they chose, and the money is then the
        money for what they are buying: the discount prorates over the picked
        parts and over no others. Absent means the kit as the merchant built it.
        A pick this kit does not contain is refused rather than dropped, because
        silently pricing a different kit than the one the shopper is looking at
        is the whole defect.
        """
        from ...product.models import ProductVariantChannelListing

        members = list(self.members.select_related("variant"))
        if variant_ids is not None:
            picked = {int(variant_id) for variant_id in variant_ids}
            unknown = picked - {member.variant_id for member in members}
            if unknown:
                raise UnknownKitMember(unknown)
            members = [member for member in members if member.variant_id in picked]
        if not members:
            raise pricing.KitRefusal("a kit with no members has no price")

        prices = {
            listing.variant_id: unit_amount(listing)
            for listing in ProductVariantChannelListing.objects.filter(
                variant_id__in=[m.variant_id for m in members],
                channel_id=channel.pk,
                price_amount__isnull=False,
            ).only("variant_id", "price_amount", "discounted_price_amount")
        }
        missing = [m.variant_id for m in members if m.variant_id not in prices]
        if missing:
            raise pricing.KitRefusal(
                f"kit members are not priced in {channel.slug}: {sorted(missing)}"
            )

        return [
            pricing.Member(
                variant=member.variant,
                unit_list_cents=pricing.to_cents(prices[member.variant_id]),
                quantity=member.quantity,
            )
            for member in members
        ]


class KitMember(models.Model):
    """One variant in a kit, and how many of it the kit contains."""

    kit = models.ForeignKey(KitConfig, related_name="members", on_delete=models.CASCADE)
    variant = models.ForeignKey(
        "product.ProductVariant",
        related_name="wsm_kit_memberships",
        on_delete=models.CASCADE,
        help_text="The SKU this kit contains. Search by product name or SKU.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many of this SKU one kit contains, and at least one.",
    )
    sort_order = models.IntegerField(
        default=0,
        help_text="Lowest first. Members with the same number fall back to the order added.",
    )

    class Meta:
        ordering = ("sort_order", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=["kit", "variant"],
                name="wsm_containers_one_row_per_kit_variant",
            )
        ]

    def __str__(self):
        # `ProductVariant.__str__` is the variant name, which is the string
        # "Base" on every single-variant product in the fleet.
        variant = self.variant
        return (
            f"{self.quantity} x {variant.product.name} "
            f"[{variant.sku or 'no SKU'}]"
        )


# --- cross-member rules -------------------------------------------------------

# The merchant's two sentences about how their kit goes together, and the only
# two the walk of 2026-09-09 found written on a kit: this part needs one of
# those, and this part cannot be sold beside that one.
REQUIRES_ONE_OF = "requires_one_of"
EXCLUDES = "excludes"
RULE_KIND_LABELS = {
    REQUIRES_ONE_OF: "Needs at least one of",
    EXCLUDES: "Cannot be sold with",
}
RULE_KIND_CHOICES = list(RULE_KIND_LABELS.items())

# The one code the storefront branches on. The KIND is data on the rule beside
# it: a storefront that wants to draw "requires" differently from "excludes"
# reads that, and one that only wants to show the merchant's sentence reads
# neither.
RULE_VIOLATION_CODE = "kit_member_rule"


class KitRulesRefused(pricing.KitRefusal):
    """A kit the merchant's own rules refuse to sell. Their words reach the shopper.

    A `KitRefusal` by inheritance, deliberately: every path that already refuses
    a kit it cannot charge refuses this one too, and only the endpoint that
    knows how to say more says more. Nothing is re-priced around a broken rule.
    """

    code = RULE_VIOLATION_CODE

    def __init__(self, rules):
        self.rules = list(rules)
        super().__init__("; ".join(rule.message for rule in self.rules))


class KitMemberRule(models.Model):
    """One member needs, or refuses, another. The merchant writes the sentence.

    The kit is carried as well as the subject's own membership so that a kit's
    rules are ONE query from the kit (`kit.rules`), which is what the checkout
    path asks for; `clean` refuses a row whose subject or targets belong to a
    different kit, so the two can never disagree.
    """

    kit = models.ForeignKey(KitConfig, related_name="rules", on_delete=models.CASCADE)
    subject = models.ForeignKey(
        KitMember,
        related_name="rules",
        on_delete=models.CASCADE,
        help_text="The part this rule is about.",
    )
    kind = models.CharField(
        max_length=20,
        choices=RULE_KIND_CHOICES,
        default=REQUIRES_ONE_OF,
        help_text=(
            "Whether the part above needs one of the parts you pick below, or "
            "cannot be sold together with them."
        ),
    )
    targets = models.ManyToManyField(
        KitMember,
        related_name="rules_targeting",
        help_text="The other parts in this kit the rule is about.",
    )
    message = models.TextField(
        help_text=(
            "What the shopper is told when this rule stops the kit going into "
            "the cart, in your own words, e.g. Requires Manual or HD Tensioner."
        ),
    )

    class Meta:
        ordering = ("pk",)
        verbose_name = "kit rule"
        verbose_name_plural = "kit rules"

    def __str__(self):
        # Never the row's own id and never a member row's id: this string is the
        # heading on the "Are you sure?" of a delete (see
        # saleor/wsm/tests/test_model_strings.py).
        variant = self.subject.variant
        return (
            f"{variant.product.name} [{variant.sku or 'no SKU'}] "
            f"{RULE_KIND_LABELS.get(self.kind, self.kind).lower()}"
        )

    def clean(self):
        """Refuse a rule reaching outside its own kit: nobody could satisfy it."""
        super().clean()
        if self.subject_id and self.kit_id and self.subject.kit_id != self.kit_id:
            raise ValidationError(
                {"subject": "that part is not in this kit"}
            )

    @property
    def target_variant_ids(self):
        """The SKUs this rule points at, as variant ids. No query when prefetched."""
        return sorted(member.variant_id for member in self.targets.all())

    def broken_by(self, present_variant_ids) -> bool:
        """Say whether this selection breaks the rule. Pure, so no kit is needed."""
        present = set(present_variant_ids)
        if self.subject.variant_id not in present:
            return False
        targets = set(self.target_variant_ids)
        if not targets:
            # A rule with nothing on the other side of it says nothing. Refusing
            # on one would make a half-typed row unsellable, and the merchant
            # screen already asks for at least one part.
            return False
        if self.kind == REQUIRES_ONE_OF:
            return not (targets & present)
        return bool(targets & present)


def evaluate_rules(kit, variant_ids):
    """Return this kit's rules, and the ones this selection breaks, from ONE read.

    Both answers together because the caller needs both and they come off the
    same rows: the broken ones refuse the add, and the whole set is what the
    response carries so a storefront can say why before a shopper tries again.

    ONE query on a kit that carries no rules, which is every kit today: Django
    skips a prefetch whose parent set came back empty. Two on a kit that does,
    whatever the number of rules, and never one per rule.
    """
    rules = list(kit.rules.select_related("subject").prefetch_related("targets"))
    return rules, [rule for rule in rules if rule.broken_by(variant_ids)]
