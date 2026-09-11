# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The two writes on a product's gate: one product, and a whole catalogue.

Its own module rather than a fourteenth class in `mutations.py`, which is 951
lines of tier-price money rules: the gate shares none of them. Same permission
(MANAGE_DISCOUNTS), same error type, same bulk cap, same module in the schema.

THE UPSERT SEMANTIC, because it is the whole contract the Dashboard toggle
round-trips on. `wsmProductGateSet` writes the row that is the merchant's
answer, and the row can say either thing: `loginRequired` true gates the
product, false publishes it EVEN ON A GATED STORE. That is not a redundant
state, it is ds's one public product out of 2,732. So a merchant flipping the
toggle back and forth writes true, then false, then true, and never has to
delete anything to get back where they started.

THE BULK PATH is why ds can go live at all. Its catalogue is 2,731 gated
products; a loop of the single mutation is 2,731 round trips and roughly 14,000
queries. This validates a whole page against itself and the tables in a fixed
number of queries and writes it in five, so the Dashboard's "Gate every product"
action is six calls of 500 and not a background job.
"""

import graphene
from django.core.exceptions import ValidationError
from django.db import transaction

from ....graphql.core.mutations import BaseMutation
from ....graphql.core.types import BaseInputObjectType, NonNullList
from ....product.models import Product
from ...dealer import models
from ..errors import WsmError
from ..types import DOC_CATEGORY_WSM
from ..utils import check_bulk_limit, error, pk_or_none
from .mutations import DEALER_PERMISSIONS
from .types import WsmProductGate


def _resolved_pks(ids, type_name, model, field, errors, *, database):
    """Global ids to row ids, in ONE existence query, or a field error each.

    Shared by both mutations, because "that is not a product" and "no product
    has that id" are the two sentences a merchant can act on and three copies of
    them is three chances for one to drift.
    """
    decoded = {}
    for value in ids:
        pk = pk_or_none(value, type_name)
        if pk is None:
            errors[field] = error(f"{value!r} is not a {type_name} id.", "invalid")
            continue
        decoded[value] = pk
    if not decoded:
        return decoded
    live = set(
        model.objects.using(database)
        .filter(pk__in=set(decoded.values()))
        .values_list("pk", flat=True)
    )
    missing = sorted({v for v in decoded.values() if v not in live})
    if missing:
        errors[field] = error(
            f"no {type_name} exists with id {missing[0]}.", "not_found"
        )
    return {value: pk for value, pk in decoded.items() if pk in live}


def _write_group_links(gate_pks_to_group_pks, *, database):
    """Replace the group links of these gates. Two queries, whatever the size.

    Delete-then-insert rather than `.set()` per row: `.set()` is two queries PER
    GATE, which on a 500-row paste is a thousand round trips to write a column
    most of those rows leave empty.
    """
    through = models.DealerProductGate.groups.through
    through.objects.using(database).filter(
        dealerproductgate_id__in=list(gate_pks_to_group_pks)
    ).delete()
    links = [
        through(dealerproductgate_id=gate_pk, dealergroup_id=group_pk)
        for gate_pk, group_pks in gate_pks_to_group_pks.items()
        for group_pk in group_pks
    ]
    if links:
        through.objects.using(database).bulk_create(links, batch_size=500)


class WsmProductGateSet(BaseMutation):
    """Set one product's gate. Creates the row or rewrites it."""

    gate = graphene.Field(
        WsmProductGate, description="The rule now stored for this product."
    )

    class Arguments:
        product = graphene.ID(required=True, description="The product to rule on.")
        login_required = graphene.Boolean(
            required=True,
            description=(
                "True: only a signed-in dealer sees the price and can buy. "
                "False: public, even when the whole store is gated."
            ),
        )
        groups = NonNullList(
            graphene.ID,
            description=(
                "Leave empty for any dealer group. Name groups to let only "
                "those groups see this product."
            ),
        )

    class Meta:
        description = "Set who may see a product's price and buy it."
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    @classmethod
    def perform_mutation(cls, _root, info, /, *, product, login_required, groups=None):
        from ....graphql.core.context import get_database_connection_name

        database = get_database_connection_name(info.context)
        errors: dict = {}
        product_pks = _resolved_pks(
            [product], "Product", Product, "product", errors, database=database
        )
        group_pks = _resolved_pks(
            list(groups or []),
            "WsmDealerGroup",
            models.DealerGroup,
            "groups",
            errors,
            database=database,
        )
        if errors:
            return cls.handle_errors(ValidationError(errors), gate=None)

        with transaction.atomic():
            row, _created = models.DealerProductGate.objects.update_or_create(
                product_id=product_pks[product],
                defaults={"login_required": login_required},
            )
            _write_group_links({row.pk: set(group_pks.values())}, database="default")
        stored = (
            models.DealerProductGate.objects.prefetch_related("groups")
            .filter(pk=row.pk)
            .first()
        )
        return cls(errors=[], gate=stored)


class WsmProductGateSetInput(BaseInputObjectType):
    product = graphene.ID(required=True, description="The product to rule on.")
    login_required = graphene.Boolean(
        required=True, description="True gates it, false publishes it."
    )
    groups = NonNullList(
        graphene.ID, description="Empty for any dealer group; named groups for those."
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmProductGateBulkSet(BaseMutation):
    """Set many products' gates in one call. One transaction, five writes."""

    count = graphene.Int(required=True, description="How many gates were written.")

    class Arguments:
        gates = NonNullList(
            WsmProductGateSetInput, required=True, description="The rules to store."
        )

    class Meta:
        description = "Set who may see many products' prices and buy them."
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    @classmethod
    def perform_mutation(cls, _root, info, /, *, gates):
        from ....graphql.core.context import get_database_connection_name

        try:
            rows = cls._clean_rows(get_database_connection_name(info.context), gates)
        except ValidationError as exc:
            # `count` is non-null, so a refusal has to carry it.
            return cls.handle_errors(exc, count=0)
        return cls(errors=[], count=cls._write(rows))

    @classmethod
    def _clean_rows(cls, database, gates):
        check_bulk_limit(gates, "gates")
        errors: dict = {}

        seen: dict = {}
        for index, row in enumerate(gates):
            if row["product"] in seen:
                errors[f"gates.{index}.product"] = error(
                    "this product is named twice in one call; it can have one "
                    "rule, so send one row for it.",
                    "unique",
                )
                continue
            seen[row["product"]] = index

        product_pks = _resolved_pks(
            list(seen), "Product", Product, "gates.product", errors, database=database
        )
        group_ids = {
            value for row in gates for value in (row.get("groups") or [])
        }
        group_pks = _resolved_pks(
            sorted(group_ids),
            "WsmDealerGroup",
            models.DealerGroup,
            "gates.groups",
            errors,
            database=database,
        )
        if errors:
            raise ValidationError(errors)

        return [
            (
                product_pks[row["product"]],
                row["login_required"],
                {group_pks[value] for value in (row.get("groups") or [])},
            )
            for row in gates
        ]

    @classmethod
    def _write(cls, rows):
        """Upsert every row, then replace every link. Five statements."""
        wanted = {product_pk: (required, groups) for product_pk, required, groups in rows}
        with transaction.atomic():
            stored = {
                row.product_id: row
                for row in models.DealerProductGate.objects.select_for_update().filter(
                    product_id__in=list(wanted)
                )
            }
            to_update = []
            for product_pk, (required, _groups) in wanted.items():
                row = stored.get(product_pk)
                if row is not None and row.login_required != required:
                    row.login_required = required
                    to_update.append(row)
            if to_update:
                models.DealerProductGate.objects.bulk_update(
                    to_update, ["login_required"], batch_size=500
                )
            created = models.DealerProductGate.objects.bulk_create(
                [
                    models.DealerProductGate(
                        product_id=product_pk, login_required=required
                    )
                    for product_pk, (required, _groups) in wanted.items()
                    if product_pk not in stored
                ],
                batch_size=500,
            )
            for row in created:
                stored[row.product_id] = row
            _write_group_links(
                {
                    stored[product_pk].pk: groups
                    for product_pk, (_required, groups) in wanted.items()
                },
                database="default",
            )
        return len(wanted)
