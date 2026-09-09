# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Restamp `wsm.price_floor` (and `compose.configurable`) from the rows.

The stamps maintain themselves from every Compose save door (see
`sync_product_stamps`), so this exists for the one input that has no such door:
the product's BASE PRICE. That lives on a core table Saleor writes through
`bulk_update` on the discount path, which fires no signal at all, and hooking it
would mean either a broad signal on a hot core write or a core edit. Neither is
worth it for a number a merchant changes by hand a few times a year, so the base
price is a known gap and this command is its answer: run it after a price
change, a price import, or a channel being added.

Idempotent by construction: `sync_product_stamps` compares before it writes, so
a second run does its reads and issues no UPDATE at all.
"""

from django.core.management.base import BaseCommand

from ...models import Fee, OptionSet, sync_product_stamps


class Command(BaseCommand):
    help = "Restamp the public price floor on configured products."

    def add_arguments(self, parser):
        parser.add_argument(
            "--product-ids",
            nargs="+",
            type=int,
            default=None,
            help=(
                "Product ids to restamp. Omit to restamp every product that "
                "carries an option set or a charge."
            ),
        )

    def handle(self, *args, **options):
        product_ids = options["product_ids"]
        if product_ids is None:
            # Only products that carry configuration: a catalog is millions of
            # rows and the ones with no Compose row have nothing to stamp.
            product_ids = sorted(
                set(OptionSet.objects.values_list("product_id", flat=True))
                | set(Fee.objects.values_list("product_id", flat=True))
            )
        restamped = 0
        for product_id in product_ids:
            if sync_product_stamps(product_id):
                restamped += 1
        self.stdout.write(
            f"{len(product_ids)} products checked, {restamped} restamped"
        )
