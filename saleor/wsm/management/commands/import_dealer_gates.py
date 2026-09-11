# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Load 5.0's `login_required` column onto the per-product gate.

WHY A COMMAND AND NOT AN IMPORTER STEP. The fork carries no 5.0 dealer importer:
the one that exists lives in the app-platform tree and writes `TierPrice` rows
through the API. Gates are a column on the 5.0 PRODUCT, not on its price list,
so they arrive with the catalogue rather than with the prices, and the catalogue
importer already knows a 5.0 product id by the time it knows a Saleor one. This
command takes the mapping AFTER that join, which is why the file is keyed on the
Saleor product id and not the 5.0 one.

THE FILE. A header row, then `product_id,login_required,group_codes`:

    product_id,login_required,group_codes
    412,1,
    413,1,fob
    414,0,

`login_required` takes 1/0, true/false, yes/no. `group_codes` is empty for "any
dealer group" and otherwise a semicolon-separated list of `DealerGroup.code`,
because a comma is the field separator and ds's group codes are free text.

`group_codes` is 5.0's `customer_group_access_link`, pivoted: one row per
product with every group that may see it. On ds that table carries 2,748 rows
across 3 of its 5 groups (WD Pallet 1,371, Jobber 1,370, Container 7), measured
2026-09-11; FOB and CIF carry none, because they are price books rather than
visibility tiers. A product with no link rows and `login_required` set gets an
empty `group_codes`, which means any dealer group, and that is 2,731 of ds's
products.

DEFAULT DENY on a row that does not parse: the row is REFUSED and named, never
guessed at. A gate guessed wrong in the permissive direction publishes a dealer
price to the public, so this command would rather import nothing.
"""

import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ....product.models import Product
from ...dealer.models import DealerGroup, DealerProductGate

TRUE = {"1", "true", "t", "yes", "y"}
FALSE = {"0", "false", "f", "no", "n", ""}


class Command(BaseCommand):
    help = "Load per-product dealer gates from a CSV of product_id,login_required,group_codes."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The CSV to read.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report, write nothing.",
        )

    def handle(self, *args, **options):
        rows, refusals = self._parse(options["path"])
        for refusal in refusals:
            self.stderr.write(refusal)
        if refusals:
            raise CommandError(
                f"{len(refusals)} rows would import a gate nobody wrote. "
                "Nothing was written: a gate guessed wrong publishes a dealer "
                "price to the public."
            )
        if options["dry_run"]:
            self.stdout.write(f"{len(rows)} gates would be written.")
            return
        written = self._write(rows)
        self.stdout.write(f"{written} gates written.")

    def _parse(self, path):
        known_groups = {
            code: pk
            for pk, code in DealerGroup.objects.values_list("pk", "code")
        }
        rows: dict[int, tuple[bool, set[int]]] = {}
        refusals: list[str] = []
        with open(path, newline="", encoding="utf-8-sig") as handle:
            for number, row in enumerate(csv.DictReader(handle), start=2):
                raw_id = (row.get("product_id") or "").strip()
                if not raw_id.isdigit():
                    refusals.append(f"line {number}: {raw_id!r} is not a product id")
                    continue
                required = (row.get("login_required") or "").strip().lower()
                if required in TRUE:
                    gated = True
                elif required in FALSE:
                    gated = False
                else:
                    refusals.append(
                        f"line {number}: {required!r} is not a yes or a no"
                    )
                    continue
                codes = [
                    code.strip()
                    for code in (row.get("group_codes") or "").split(";")
                    if code.strip()
                ]
                unknown = [code for code in codes if code not in known_groups]
                if unknown:
                    refusals.append(
                        f"line {number}: no dealer group has the code "
                        f"{unknown[0]!r}; add it first"
                    )
                    continue
                rows[int(raw_id)] = (gated, {known_groups[code] for code in codes})

        live = set(
            Product.objects.filter(pk__in=list(rows)).values_list("pk", flat=True)
        )
        for product_id in sorted(set(rows) - live):
            refusals.append(f"no product exists with id {product_id}")
        return {pk: value for pk, value in rows.items() if pk in live}, refusals

    @transaction.atomic
    def _write(self, rows):
        """The same upsert the bulk mutation does, in the same query budget."""
        stored = {
            gate.product_id: gate
            for gate in DealerProductGate.objects.select_for_update().filter(
                product_id__in=list(rows)
            )
        }
        changed = []
        for product_id, (gated, _groups) in rows.items():
            gate = stored.get(product_id)
            if gate is not None and gate.login_required != gated:
                gate.login_required = gated
                changed.append(gate)
        if changed:
            DealerProductGate.objects.bulk_update(
                changed, ["login_required"], batch_size=500
            )
        created = DealerProductGate.objects.bulk_create(
            [
                DealerProductGate(product_id=product_id, login_required=gated)
                for product_id, (gated, _groups) in rows.items()
                if product_id not in stored
            ],
            batch_size=500,
        )
        for gate in created:
            stored[gate.product_id] = gate

        through = DealerProductGate.groups.through
        gate_pks = [stored[product_id].pk for product_id in rows]
        through.objects.filter(dealerproductgate_id__in=gate_pks).delete()
        through.objects.bulk_create(
            [
                through(
                    dealerproductgate_id=stored[product_id].pk, dealergroup_id=group_pk
                )
                for product_id, (_gated, groups) in rows.items()
                for group_pk in groups
            ],
            batch_size=500,
        )
        return len(rows)
