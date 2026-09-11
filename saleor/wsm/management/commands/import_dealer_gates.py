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

`--categories` reads `category_id` instead and writes `DealerCategoryGate`,
which is where 5.0's category rows land: ds has 15 category visibility rows and
5 category login gates, and the fleet has 127 login rows over 76 tenants. Same
file shape, same rules, same refusals, because they are the same two answers
about a different row.

`group_codes` is 5.0's `customer_group_access_link`, pivoted: one row per
product or category with every group that may see it. On ds that table carries 2,748 rows
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

from ....product.models import Category, Product
from ...dealer.models import DealerCategoryGate, DealerGroup, DealerProductGate

TRUE = {"1", "true", "t", "yes", "y"}
FALSE = {"0", "false", "f", "no", "n", ""}


class Command(BaseCommand):
    help = (
        "Load dealer gates from a CSV of <product|category>_id,login_required,"
        "group_codes."
    )

    def add_arguments(self, parser):
        parser.add_argument("path", help="The CSV to read.")
        parser.add_argument(
            "--categories",
            action="store_true",
            help=(
                "Read category_id instead of product_id and write category "
                "gates. 5.0's category rows come from the same two tables and "
                "mean the same two things, so they come through the same door."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report, write nothing.",
        )

    def handle(self, *args, **options):
        self.categories = options["categories"]
        self.column = "category_id" if self.categories else "product_id"
        self.gate_model = DealerCategoryGate if self.categories else DealerProductGate
        self.target_model = Category if self.categories else Product
        self.target_field = "category_id" if self.categories else "product_id"
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
            code: pk for pk, code in DealerGroup.objects.values_list("pk", "code")
        }
        rows: dict[int, tuple[bool, set[int]]] = {}
        refusals: list[str] = []
        with open(path, newline="", encoding="utf-8-sig") as handle:
            for number, row in enumerate(csv.DictReader(handle), start=2):
                raw_id = (row.get(self.column) or "").strip()
                if not raw_id.isdigit():
                    refusals.append(f"line {number}: {raw_id!r} is not a {self.column}")
                    continue
                required = (row.get("login_required") or "").strip().lower()
                if required in TRUE:
                    gated = True
                elif required in FALSE:
                    gated = False
                else:
                    refusals.append(f"line {number}: {required!r} is not a yes or a no")
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
            self.target_model.objects.filter(pk__in=list(rows)).values_list(
                "pk", flat=True
            )
        )
        name = self.target_model.__name__.lower()
        for target_id in sorted(set(rows) - live):
            refusals.append(f"no {name} exists with id {target_id}")
        return {pk: value for pk, value in rows.items() if pk in live}, refusals

    @transaction.atomic
    def _write(self, rows):
        """The same upsert the bulk mutation does, in the same query budget."""
        model = self.gate_model
        key = self.target_field
        stored = {
            getattr(gate, key): gate
            for gate in model.objects.select_for_update().filter(
                **{f"{key}__in": list(rows)}
            )
        }
        changed = []
        for target_id, (gated, _groups) in rows.items():
            gate = stored.get(target_id)
            if gate is not None and gate.login_required != gated:
                gate.login_required = gated
                changed.append(gate)
        if changed:
            model.objects.bulk_update(changed, ["login_required"], batch_size=500)
        created = model.objects.bulk_create(
            [
                model(**{key: target_id}, login_required=gated)
                for target_id, (gated, _groups) in rows.items()
                if target_id not in stored
            ],
            batch_size=500,
        )
        for gate in created:
            stored[getattr(gate, key)] = gate

        through = model.groups.through
        column = f"{model._meta.model_name}_id"
        gate_pks = [stored[target_id].pk for target_id in rows]
        through.objects.filter(**{f"{column}__in": gate_pks}).delete()
        through.objects.bulk_create(
            [
                through(**{column: stored[target_id].pk, "dealergroup_id": group_pk})
                for target_id, (_gated, groups) in rows.items()
                for group_pk in groups
            ],
            batch_size=500,
        )
        return len(rows)
