# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Import one WSM 5.0 site's option sets into Compose's own tables.

5.0 keeps a configurator in four tables: `product_option_set` (the question),
`product_option_link` (which products ask it), `product_option_value` (the
answers) and `product_fee` (a charge the merchant attaches to a product). This
reads those for ONE site and writes `wsm_compose` rows plus the `wsm_dealer`
rows an option-value tier needs. Nothing here creates a Saleor variant: an
option value is never a variant (Dana, 2026-09-05), and the fee variant a
checkout line points at is minted by `Fee.ensure_variant` at add time, not here.

Two shapes in the 5.0 data decide the whole mapping:

1. A SET IS SHARED, A COMPOSE SET IS NOT. `product_option_link` is many-to-many,
   so one 5.0 set linked to four products becomes four `OptionSet` rows. Compose
   hangs a set off one product on purpose (the PDP reads one product's rows in
   one query), so the expansion happens here rather than in the hot path.

2. A DEALER TIER IS A DUPLICATE VALUE ROW. 5.0 has no per-option-value tier
   table. It repeats the value once per buyer group and puts the group's name in
   `desc`: ("Black", desc "Retail", 0.00), ("Black", desc "Dealer 1", 0.00),
   ("Black", desc "Dealer 2", 0.00); ("Red", "Retail", 34.00), ("Red",
   "Dealer 1", 32.30), ("Red", "Dealer 2", 21.97). The Retail row is the
   `OptionValue`; every sibling whose `desc` names a customer group of this site
   becomes one `DealerTierOptionPrice`. A `desc` that names no group is left on
   the retail row it belongs to and reported, never guessed at.

Idempotent by natural key, so a second run writes nothing: an `OptionSet` is
identified by (product, name), a value by (option set, name, sku fragment), a
tier by (value, group). Both are unique in the source, measured on Fuel Lab
before this was written. ponytail: the ceiling is a RENAME in 5.0, which reads
as a new row and leaves the old one behind; the report counts those as `stale`
rather than deleting, because deleting a merchant's live configurator on the
strength of a rename is the worse failure. An `--include-hidden` run is the only
way a hidden 5.0 set reaches the storefront.

Reads 5.0 through the `mysql` client, one statement per table, each returning a
single JSON document. No driver is added to the image for an importer that runs
once per tenant, and JSON means a value named with a tab or a newline survives
the trip, which a --batch tab-separated read does not. The password is passed in
`MYSQL_PWD` so it is never an argv anyone can see in `ps`.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from .....product.models import Product, ProductVariant
from ....dealer.models import DealerGroup, TierPrice
from ... import pricing
from ...models import DealerTierOptionPrice, Fee, OptionSet, OptionValue

# The product metafield the PDP configurator gates on. The storefront asks a
# product nothing without it, so an imported product that carries a set and not
# this string is a silent no-op on the shelf.
CONFIGURABLE_METAFIELD = "compose.configurable"
CONFIGURABLE_VALUE = "true"

# 5.0's `product_option_set.type` enum, one to one onto Compose prompt types.
# A type 5.0 grows that is not here stops the import rather than defaulting to
# a dropdown: guessing the prompt is guessing what the shopper is asked.
PROMPT_BY_50_TYPE = {
    "enum": pricing.CHOICE_ONE,
    "multi-enum": pricing.CHOICE_MANY,
    "text": "text",
    "image": "image",
    "date": "date",
    "datetime": "datetime",
}

# The `desc` on the row that is not a dealer tier.
RETAIL_DESC = "Retail"


def _mysql_json(host, user, database, statement):
    """One statement, one JSON document, through the mysql client.

    `group_concat_max_len` is raised because JSON_ARRAYAGG is built on it and
    silently TRUNCATES at 1 KB by default, which would produce a short import
    that looked like a clean one.
    """
    sql = f"SET SESSION group_concat_max_len=1073741824; USE `{database}`; {statement}"
    proc = subprocess.run(
        [
            "mysql",
            "-h",
            host,
            "-u",
            user,
            "--batch",
            "--raw",
            "-N",
            "--connect-timeout=20",
            "-e",
            sql,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise CommandError(f"5.0 read failed: {proc.stderr.strip()[:400]}")
    out = proc.stdout.strip()
    if not out or out == "NULL":
        return []
    return json.loads(out)


def read_50(host, user, database, site_id):
    """Everything this import needs from 5.0, as plain dicts.

    Separated from `apply` so the tests exercise the mapping on a fixture
    payload and never need a MySQL server.
    """
    site = int(site_id)
    set_columns = """
        'set_id', s.id, 'sku', p.stockid, 'name', s.name, 'label', s.label,
        'type', s.type, 'required', s.required, 'hidden', s.hidden,
        'priority', s.priority, 'deselect', s.deselect,
        'description', s.description"""
    # A 5.0 set reaches a product two ways: bound to it directly, or through the
    # link table. Read as two statements rather than a UNION so each returns one
    # JSON document; a UNION returns one per branch and there is no such thing
    # as half a JSON parse.
    bound = _mysql_json(
        host,
        user,
        database,
        f"""
        SELECT JSON_ARRAYAGG(JSON_OBJECT({set_columns}))
        FROM product_option_set s
        JOIN product p ON p.id = s.product
        WHERE s.site = {site} AND s.product > 0;
    """,
    )
    linked = _mysql_json(
        host,
        user,
        database,
        f"""
        SELECT JSON_ARRAYAGG(JSON_OBJECT({set_columns}))
        FROM product_option_link l
        JOIN product_option_set s ON s.id = l.option_set
        JOIN product p ON p.id = l.product
        WHERE s.site = {site};
    """,
    )
    values = _mysql_json(
        host,
        user,
        database,
        f"""
        SELECT JSON_ARRAYAGG(JSON_OBJECT(
            'set_id', v.product_option_set, 'value_id', v.id, 'name', v.name,
            'desc', v.desc, 'price', CAST(v.price AS CHAR), 'sku', v.sku,
            'priority', v.priority, 'image', v.image,
            'image_ext', i.extension))
        FROM product_option_value v
        JOIN product_option_set s ON s.id = v.product_option_set
        LEFT JOIN image i ON i.id = v.image
        WHERE s.site = {site};
    """,
    )
    groups = _mysql_json(
        host,
        user,
        database,
        f"""
        SELECT JSON_ARRAYAGG(JSON_OBJECT(
            'id', id, 'name', name, 'is_price_group', is_price_group,
            'active', active))
        FROM customer_group WHERE store = {site};
    """,
    )
    fees = _mysql_json(
        host,
        user,
        database,
        f"""
        SELECT JSON_ARRAYAGG(JSON_OBJECT(
            'sku', p.stockid, 'fee', CAST(f.fee AS CHAR),
            'fee_label', f.fee_label, 'fee_sku', f.sku,
            'return_first', f.return_first,
            'return_first_label', f.return_first_label))
        FROM product_fee f JOIN product p ON p.id = f.product
        WHERE p.site = {site};
    """,
    )
    tier_prices = _mysql_json(
        host,
        user,
        database,
        f"""
        SELECT JSON_ARRAYAGG(JSON_OBJECT(
            'sku', p.stockid, 'group', g.name,
            'price', CAST(t.price AS CHAR), 'quantity', t.quantity))
        FROM product_tiered_price t
        JOIN customer_group g ON g.id = t.customer_group
        JOIN product p ON p.id = t.product
        WHERE g.store = {site} AND p.site = {site};
    """,
    )
    return {
        "sets": list(bound) + list(linked),
        "values": values or [],
        "groups": groups or [],
        "fees": fees or [],
        "tier_prices": tier_prices or [],
    }


def _image_url(row, image_base):
    """5.0 keeps the id and the extension and derives the path; `image.url` is empty."""
    if not image_base or not row.get("image") or not row.get("image_ext"):
        return ""
    return f"{image_base.rstrip('/')}/F{row['image']}.{row['image_ext']}"


class _Report(dict):
    def bump(self, key, n=1):
        self[key] = self.get(key, 0) + n


def apply(payload, *, channel_slug, include_hidden=False, image_base="", dry_run=False):
    """Write the 5.0 payload into Compose's tables. Returns the count report.

    One transaction: a half-imported configurator prices a shopper's cart on
    half a question.
    """
    report = _Report()
    report["unmatched_skus"] = []
    report["unknown_desc"] = []
    report["orphan_tier_keys"] = []

    price_groups = {
        row["name"]: slugify(row["name"])
        for row in payload["groups"]
        if int(row.get("is_price_group") or 0) and int(row.get("active") or 0)
    }

    # SKU to Saleor product, scoped to the channel this tenant sells in: the
    # bake-off database carries another tenant's 48,000 products in another
    # channel and a stock number is only unique inside a catalog.
    by_sku = dict(
        ProductVariant.objects.filter(
            product__channel_listings__channel__slug=channel_slug
        ).values_list("sku", "product_id")
    )
    if not by_sku:
        raise CommandError(f"channel {channel_slug!r} lists no products with a SKU")

    values_by_set = defaultdict(list)
    for row in payload["values"]:
        values_by_set[row["set_id"]].append(row)

    with transaction.atomic():
        groups = {}
        for name, code in sorted(price_groups.items()):
            group, created = DealerGroup.objects.get_or_create(
                code=code, defaults={"name": name}
            )
            groups[name] = group
            report.bump("groups_created" if created else "groups_unchanged")

        configurable = set()
        for row in payload["sets"]:
            if int(row.get("hidden") or 0) and not include_hidden:
                report.bump("sets_hidden_refused")
                continue
            product_id = by_sku.get(row["sku"])
            if product_id is None:
                report.bump("sets_unmatched")
                if row["sku"] not in report["unmatched_skus"]:
                    report["unmatched_skus"].append(row["sku"])
                continue
            prompt = PROMPT_BY_50_TYPE.get(row["type"])
            if prompt is None:
                raise CommandError(
                    f"5.0 option set {row['set_id']} has type {row['type']!r}, "
                    "which has no Compose prompt type"
                )
            fields = {
                "label": row.get("label") or "",
                "prompt_type": prompt,
                "required": bool(int(row.get("required") or 0)),
                "note": row.get("description") or "",
                "sort_order": int(row.get("priority") or 0),
            }
            option_set = OptionSet.objects.filter(
                product_id=product_id, name=row["name"]
            ).first()
            if option_set is None:
                option_set = OptionSet(
                    product_id=product_id, name=row["name"], **fields
                )
                option_set.save()
                report.bump("sets_created")
            elif any(getattr(option_set, k) != v for k, v in fields.items()):
                for k, v in fields.items():
                    setattr(option_set, k, v)
                option_set.save()
                report.bump("sets_updated")
            else:
                report.bump("sets_unchanged")
            configurable.add(product_id)

            _import_values(
                option_set,
                values_by_set.get(row["set_id"], []),
                price_groups,
                image_base,
                report,
            )

        _import_fees(payload["fees"], by_sku, report)
        _import_tier_prices(
            payload["tier_prices"], by_sku, groups, price_groups, report
        )

        for product in Product.objects.filter(pk__in=configurable):
            if product.metadata.get(CONFIGURABLE_METAFIELD) == CONFIGURABLE_VALUE:
                report.bump("metafield_unchanged")
                continue
            product.metadata[CONFIGURABLE_METAFIELD] = CONFIGURABLE_VALUE
            product.save(update_fields=["metadata"])
            report.bump("metafield_written")

        if dry_run:
            transaction.set_rollback(True)
            report["rolled_back"] = True
    return report


def _import_values(option_set, rows, price_groups, image_base, report):
    """Write the Retail row as the value and each dealer sibling as a tier row."""
    retail = [r for r in rows if (r.get("desc") or RETAIL_DESC) == RETAIL_DESC]
    tiers = defaultdict(list)
    for row in rows:
        desc = row.get("desc") or RETAIL_DESC
        if desc == RETAIL_DESC:
            continue
        if desc not in price_groups:
            # A `desc` naming no customer group is prose on the value, not a
            # tier, and there is no retail row it could be a tier OF.
            report.bump("values_desc_not_a_group")
            entry = f"{option_set.name}: {desc}"
            if entry not in report["unknown_desc"]:
                report["unknown_desc"].append(entry)
            continue
        tiers[(row["name"], row.get("sku") or "")].append((desc, row["price"]))

    seen = set()
    for row in retail:
        key = (row["name"], row.get("sku") or "")
        seen.add(key)
        fields = {
            "price_delta": Decimal(row["price"]),
            "image_url": _image_url(row, image_base),
            "sort_order": int(row.get("priority") or 0),
        }
        value = OptionValue.objects.filter(
            option_set=option_set, name=row["name"], sku_fragment=key[1]
        ).first()
        if value is None:
            value = OptionValue(
                option_set=option_set, name=row["name"], sku_fragment=key[1], **fields
            )
            value.save()
            report.bump("values_created")
        elif any(getattr(value, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(value, k, v)
            value.save()
            report.bump("values_updated")
        else:
            report.bump("values_unchanged")

        for desc, amount in tiers.get(key, []):
            code = price_groups[desc]
            delta = Decimal(amount)
            tier = DealerTierOptionPrice.objects.filter(
                option_value=value, tier_group=code
            ).first()
            if tier is None:
                DealerTierOptionPrice.objects.create(
                    option_value=value, tier_group=code, price_delta=delta
                )
                report.bump("tiers_created")
            elif tier.price_delta != delta:
                tier.price_delta = delta
                tier.save()
                report.bump("tiers_updated")
            else:
                report.bump("tiers_unchanged")

    # A dealer row whose retail sibling is missing has no value to hang a delta
    # on. Counted rather than dropped: 5.0 having priced a buyer group on an
    # answer nobody can pick is the merchant's to see, not ours to invent.
    for key, rows_for_key in tiers.items():
        if key in seen:
            continue
        report.bump("tiers_orphaned", len(rows_for_key))
        entry = f"{option_set.name}: {key[0]!r}/{key[1]!r}"
        if entry not in report["orphan_tier_keys"]:
            report["orphan_tier_keys"].append(entry)

    stale = sum(
        1
        for name, fragment in OptionValue.objects.filter(
            option_set=option_set
        ).values_list("name", "sku_fragment")
        if (name, fragment) not in seen
    )
    report.bump("values_stale", stale)


def _import_fees(rows, by_sku, report):
    for row in rows:
        product_id = by_sku.get(row["sku"])
        if product_id is None:
            report.bump("fees_unmatched")
            continue
        fields = {
            "sku": row.get("fee_sku") or "",
            "basis": pricing.FIXED,
            "amount": Decimal(row["fee"]),
            "apply_to": pricing.PER_UNIT,
            # 5.0's `return_first` is the merchant offering the shopper a way
            # out of the charge, which is exactly Compose's `required=False`
            # plus the label the decline button carries.
            "required": not int(row.get("return_first") or 0),
            "decline_label": row.get("return_first_label") or "",
        }
        label = row.get("fee_label") or ""
        fee = Fee.objects.filter(product_id=product_id, label=label).first()
        if fee is None:
            Fee.objects.create(product_id=product_id, label=label, **fields)
            report.bump("fees_created")
        elif any(getattr(fee, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(fee, k, v)
            fee.save()
            report.bump("fees_updated")
        else:
            report.bump("fees_unchanged")


def _import_tier_prices(rows, by_sku, groups, price_groups, report):
    """5.0's per-product dealer price, onto the product's base variant.

    The base variant is the one whose SKU is the stock number itself. The
    sandbox catalog also carries `<stockid>:<axis>-<n>-<fragment>` variants from
    an older import that made an option value a variant; those are never a
    product's dealer price and the exact-SKU match leaves them alone.
    """
    for row in rows:
        product_id = by_sku.get(row["sku"])
        group = groups.get(row["group"])
        if product_id is None or group is None:
            report.bump("tier_prices_unmatched")
            continue
        variant = ProductVariant.objects.filter(
            product_id=product_id, sku=row["sku"]
        ).first()
        if variant is None:
            report.bump("tier_prices_unmatched")
            continue
        amount = Decimal(row["price"])
        min_quantity = max(1, int(row.get("quantity") or 1))
        tier = TierPrice.objects.filter(
            variant=variant, group=group, min_quantity=min_quantity
        ).first()
        if tier is None:
            TierPrice.objects.create(
                variant=variant, group=group, min_quantity=min_quantity, amount=amount
            )
            report.bump("tier_prices_created")
        elif tier.amount != amount:
            tier.amount = amount
            tier.save()
            report.bump("tier_prices_updated")
        else:
            report.bump("tier_prices_unchanged")


class Command(BaseCommand):
    help = "Import one WSM 5.0 site's option sets, fees and dealer tiers into Compose."

    def add_arguments(self, parser):
        parser.add_argument("--host", required=True, help="5.0 MySQL reader endpoint")
        parser.add_argument("--user", required=True, help="5.0 MySQL user")
        parser.add_argument("--database", required=True, help="e.g. wsm_live_fub")
        parser.add_argument("--site-id", required=True, type=int)
        parser.add_argument(
            "--channel", required=True, help="Saleor channel slug this tenant sells in"
        )
        parser.add_argument(
            "--image-base",
            default="",
            help="Base URL for option-value swatches; blank leaves image_url empty",
        )
        parser.add_argument("--include-hidden", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not os.environ.get("MYSQL_PWD"):
            raise CommandError("MYSQL_PWD is not set; the 5.0 password is read from it")
        payload = read_50(
            options["host"], options["user"], options["database"], options["site_id"]
        )
        report = apply(
            payload,
            channel_slug=options["channel"],
            include_hidden=options["include_hidden"],
            image_base=options["image_base"],
            dry_run=options["dry_run"],
        )
        self.stdout.write("read from 5.0:")
        for key in ("sets", "values", "groups", "fees", "tier_prices"):
            self.stdout.write(f"  {key}: {len(payload[key])}")
        self.stdout.write("written:")
        for key, count in sorted(report.items()):
            if not isinstance(count, list):
                self.stdout.write(f"  {key}: {count}")
                continue
            shown = ", ".join(str(x) for x in count[:10])
            more = f" (+{len(count) - 10} more)" if len(count) > 10 else ""
            self.stdout.write(
                f"  {key}: {len(count)}" + (f" [{shown}{more}]" if count else "")
            )
