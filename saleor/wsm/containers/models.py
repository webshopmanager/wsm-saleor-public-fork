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
"""

import json

from django.core.exceptions import ValidationError
from django.db import models

from . import pricing

DISCOUNT_KIND_CHOICES = [(k, k) for k in pricing.DISCOUNT_KINDS]

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

    def stamp(self):
        """One query for the rows with their collections, one write per collection."""
        for config in self.select_related("collection"):
            config.stamp_collection()


class SeriesConfig(models.Model):
    """What a Collection needs to behave as a series: one brand, and the axes."""

    collection = models.OneToOneField(
        "product.Collection", related_name="wsm_series", on_delete=models.CASCADE
    )
    # A series spans many categories and exactly ONE brand (ruled 2026-09-08).
    brand = models.CharField(max_length=250)
    # Attribute slugs, in the order the configurator asks them.
    axes = models.JSONField(default=list, blank=True)
    partitioning_axis = models.CharField(max_length=250)
    miss_message = models.TextField(blank=True)
    published = models.BooleanField(default=False)

    objects = models.Manager.from_queryset(SeriesConfigQuerySet)()

    class Meta:
        ordering = ("pk",)

    def __str__(self):
        return f"series: {self.collection.slug}"

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

    def stamp_collection(self):
        """Publish the series facts on the Collection, for readers we do not own.

        The storefront's collection page and the search indexer both read this;
        neither gets a new GraphQL field, because Saleor already returns a
        Collection's metadata.
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


class KitConfig(models.Model):
    """What a Collection needs to behave as a kit: a discount, and members."""

    collection = models.OneToOneField(
        "product.Collection", related_name="wsm_kit", on_delete=models.CASCADE
    )
    discount_kind = models.CharField(
        max_length=10, choices=DISCOUNT_KIND_CHOICES, default=pricing.FIXED
    )
    discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text="Currency, or percent."
    )
    freight_class = models.CharField(max_length=50, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("pk",)

    def __str__(self):
        return f"kit: {self.collection.slug}"

    def pricing_members(self, channel):
        """Price this kit's members in one channel, in two queries, in member order.

        Two rather than one because a channel listing per member is a second
        table and joining it would still return a row per member: the second
        query is the cheap half of the pair, and it keeps the member rows from
        multiplying if a variant is ever listed in more than one channel.
        """
        from ...product.models import ProductVariantChannelListing

        members = list(self.members.select_related("variant"))
        if not members:
            raise pricing.KitRefusal("a kit with no members has no price")

        prices = dict(
            ProductVariantChannelListing.objects.filter(
                variant_id__in=[m.variant_id for m in members],
                channel_id=channel.pk,
                price_amount__isnull=False,
            ).values_list("variant_id", "price_amount")
        )
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
    )
    quantity = models.PositiveIntegerField(default=1)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=["kit", "variant"],
                name="wsm_containers_one_row_per_kit_variant",
            )
        ]

    def __str__(self):
        return f"{self.quantity} x {self.variant}"
