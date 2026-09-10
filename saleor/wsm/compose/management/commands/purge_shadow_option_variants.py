# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Delete the shadow variants an older import minted for option values.

An option value is never a Saleor variant (Dana, 2026-09-05). The 2026-08-31
app-platform import priced each choice as a Saleor variant with its own channel
listing, and those rows are still in the catalog: 607 of them on the Fuel Lab
bake-off copy. Compose now owns the value and its money in its own tables and
`import_option_sets_50` matches on the exact stock number, so nothing reads the
shadow rows any more. What is left is a merchant-visible variant a shopper can
pick and buy at a price no configurator agrees with, so it is deleted, not kept.

THE MARKER is the public metadata key `option_set_id`, which only that import
writes. Measured on `saleor_bakeoff_fub` before this was written: 607 variants
carry it, all 607 in channel `fuelab`, all 607 on the 97 products the import
made configurable, and no other variant of the 48,914 in that database carries
it. The `<stock number>:<set>-<n>-<value>` SKU shape is NOT the marker and is
not used as one: 48,596 variants in the same database have a colon in the SKU
for unrelated reasons, so keying on the SKU would delete most of the catalog.

Default-deny, three ways, because the failure mode is an unrecoverable delete:

1. A candidate on any ORDER LINE is never deleted. It is reported and left in
   place: an order line is a thing that happened, and a merchant reading last
   year's order needs the row it points at.
2. A candidate anything else still holds (a live checkout line, a stock row, a
   product's `default_variant`, or the last variant its product has) is
   AMBIGUOUS. Ambiguous is reported and not deleted; it is not a refusal,
   because one odd row should not strand the other 606.
3. `--apply` re-reads the marker inside the transaction and compares a
   per-product census of everything that is NOT being deleted, taken before and
   after. Any candidate without the marker, or any change to a product's
   surviving variant count, refuses the whole run and rolls back.

Dry run by default. ponytail: the ceiling is that this reads the marker, not the
merchant's intent, so a variant an operator hand-tagged `option_set_id` would be
swept with the rest. The upgrade path if that ever happens is to require the
matching `wsm_compose.OptionSet` row to exist as well; today, on measured data,
that second key would select the same 607 rows and cost a join per run.
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

from .....channel.models import Channel
from .....checkout.models import CheckoutLine
from .....order.models import OrderLine
from .....product.models import Product, ProductVariant
from .....warehouse.models import Stock

MARKER = "option_set_id"


def candidates(channel_slug):
    """Every variant the older import minted, inside one tenant's channel."""
    return ProductVariant.objects.filter(
        product__channel_listings__channel__slug=channel_slug,
        metadata__has_key=MARKER,
    ).distinct()


def _surviving_census(doomed_ids):
    """Per-product count of every variant that is NOT in `doomed_ids`."""
    return dict(
        ProductVariant.objects.exclude(id__in=doomed_ids)
        .values("product_id")
        .annotate(n=Count("id"))
        .values_list("product_id", "n")
    )


def survey(channel_slug):
    """Split the candidates into deletable, referenced-by-order and ambiguous."""
    rows = dict(candidates(channel_slug).values_list("id", "sku"))
    ids = set(rows)

    ordered = set(
        OrderLine.objects.filter(variant_id__in=ids).values_list(
            "variant_id", flat=True
        )
    )
    reasons = {
        "in a checkout": set(
            CheckoutLine.objects.filter(variant_id__in=ids).values_list(
                "variant_id", flat=True
            )
        ),
        "carries stock": set(
            Stock.objects.filter(product_variant_id__in=ids).values_list(
                "product_variant_id", flat=True
            )
        ),
        "is a product default variant": set(
            Product.objects.filter(default_variant_id__in=ids).values_list(
                "default_variant_id", flat=True
            )
        ),
    }
    surviving = _surviving_census(ids)
    bare = {
        product_id
        for product_id in ProductVariant.objects.filter(id__in=ids).values_list(
            "product_id", flat=True
        )
        if not surviving.get(product_id)
    }
    reasons["is its product's last variant"] = {
        variant_id
        for variant_id, product_id in ProductVariant.objects.filter(
            id__in=ids
        ).values_list("id", "product_id")
        if product_id in bare
    }

    ambiguous = set().union(*reasons.values()) - ordered
    deletable = ids - ordered - ambiguous

    why: Counter[str] = Counter()
    for reason, hit in reasons.items():
        for _variant_id in hit - ordered:
            why[reason] += 1

    return {
        "candidates": len(ids),
        "deletable": len(deletable),
        "referenced_by_order": len(ordered),
        "ambiguous": len(ambiguous),
        "ambiguous_because": dict(why),
        "referenced_by_order_skus": sorted(rows[i] for i in ordered),
        "ambiguous_skus": sorted(rows[i] for i in ambiguous),
        "deletable_ids": deletable,
    }


def purge(channel_slug):
    """Delete the unambiguous candidates, or refuse and write nothing."""
    report = survey(channel_slug)
    doomed = report["deletable_ids"]

    with transaction.atomic():
        found = list(
            ProductVariant.objects.filter(id__in=doomed).values("id", "sku", "metadata")
        )
        missing = len(doomed) - len(found)
        if missing:
            raise CommandError(
                f"refusing: {missing} candidate(s) vanished between the survey and "
                "the delete; nothing was written"
            )
        unmarked = [
            row["sku"] for row in found if MARKER not in (row["metadata"] or {})
        ]
        if unmarked:
            raise CommandError(
                f"refusing: {len(unmarked)} candidate(s) do not carry the "
                f"{MARKER!r} marker ({', '.join(sorted(unmarked)[:5])}); "
                "nothing was written"
            )

        before = _surviving_census(doomed)
        ProductVariant.objects.filter(id__in=doomed).delete()
        after = _surviving_census([])
        drifted = sorted(
            product_id
            for product_id in set(before) | set(after)
            if before.get(product_id, 0) != after.get(product_id, 0)
        )
        if drifted:
            raise CommandError(
                f"refusing: deleting {len(doomed)} shadow variant(s) also changed the "
                f"surviving variant count of {len(drifted)} product(s) "
                f"({drifted[:5]}); rolled back, nothing was written"
            )

    report["deleted"] = len(doomed)
    return report


class Command(BaseCommand):
    help = "Delete option-value shadow variants left by the 2026-08-31 import"

    def add_arguments(self, parser):
        parser.add_argument(
            "--site", required=True, help="Saleor channel slug this tenant sells in"
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the unambiguous candidates; without it this only counts",
        )

    def handle(self, *args, **options):
        slug = options["site"]
        if not Channel.objects.filter(slug=slug).exists():
            raise CommandError(f"no channel with slug {slug!r}")

        report = purge(slug) if options["apply"] else survey(slug)
        report.pop("deletable_ids")

        self.stdout.write(f"channel {slug}, marker {MARKER}:")
        for key, value in report.items():
            if isinstance(value, list):
                shown = ", ".join(value[:10])
                more = f" (+{len(value) - 10} more)" if len(value) > 10 else ""
                self.stdout.write(
                    f"  {key}: {len(value)}" + (f" [{shown}{more}]" if value else "")
                )
            else:
                self.stdout.write(f"  {key}: {value}")
        if not options["apply"]:
            self.stdout.write("dry run: nothing was deleted. Re-run with --apply.")
