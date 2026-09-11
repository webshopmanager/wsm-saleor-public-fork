# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The thirteen dealer mutations.

Four on a group, three on a customer, five on a tier price, one on the settings
singleton.

Three rules here are the whole reason this file is longer than a declaration:

1. **A tier amount is money that gets CHARGED.** The column carries three
   decimal places to match `CheckoutLine.price_override`, the merchant screen
   takes two (`dealer/admin.py:106`), and the floor is one cent rather than
   "above zero" because 0.004 is a positive number that charges 0.00. Both
   refusals are field errors with their own code. The `CheckConstraint`
   `wsm_dealer_tier_amount_at_least_a_cent` is the backstop under them, for the
   writers that never call `full_clean()`; a mutation that let a row reach it
   would hand the merchant a 500 instead of a sentence.

2. **A group code is a name other tables point at by string.** wsm.compose
   stores `tier_group` as a bare CharField, so a code that is deleted or
   duplicated silently changes what a compose delta prices against. Deleting a
   group with customers is refused (`on_delete=PROTECT`) before the database
   raises, so the merchant reads a sentence rather than a traceback.

3. **A 300-row paste is the import path, not a loop.** `wsmTierPriceBulkCreate`
   validates the whole payload against itself and the stored rows in a fixed
   number of queries and writes it with ONE `bulk_create`.
"""

from decimal import Decimal

import graphene
from django.core.exceptions import ValidationError
from django.db import transaction

from ....graphql.core.context import get_database_connection_name
from ....graphql.core.mutations import (
    BaseMutation,
    DeprecatedModelMutation,
    ModelBulkDeleteMutation,
    ModelDeleteMutation,
)
from ....graphql.core.types import BaseInputObjectType, NonNullList
from ....permission.enums import DiscountPermissions
from ...dealer import models
from ...money import to_money
from ..errors import WsmError
from ..scalars import WsmDecimal
from ..types import DOC_CATEGORY_WSM
from ..utils import TypedIdMixin, error, pk_or_none
from .types import (
    WsmDealerCustomer,
    WsmDealerGroup,
    WsmDealerSettings,
    WsmTierPrice,
)

# ponytail: every mutation here reuses MANAGE_DISCOUNTS rather than adding a
# `WsmPermissions` enum, because that enum lives in a core file and our
# codenames would have to reach graphene's `PermissionEnum` to be assignable in
# the Dashboard's own permission-group screens. Ceiling: anyone who can edit a
# voucher can edit a dealer price. Upgrade path: append a `WsmPermissions` class
# to `PERMISSIONS_ENUMS` from `ready()` before the schema is built, at which
# point the existing `create_wsm_permissions` receiver already creates the
# `wsm_*` permission rows those codenames would bind to.
DEALER_PERMISSIONS = (DiscountPermissions.MANAGE_DISCOUNTS,)

# One call is one paste. Stock caps its own bulk create the same way
# (`MAX_ORDERS = 50`, `saleor/graphql/order/bulk_mutations/order_bulk_create.py:86`)
# and 50 is the shape of an order import, not of a price list: live data is 304
# tier rows on one group (`dealer/admin.py:32`), so the cap is set above the
# paste this mutation exists for and below the payload that would hold the
# request open building instances nobody can read back.
MAX_TIER_PRICES = 500


def _check_bulk_limit(rows) -> None:
    """One paste is one call, and the refusal is the same on every bulk path."""
    if len(rows) > MAX_TIER_PRICES:
        raise ValidationError(
            {
                "tierPrices": error(
                    f"{len(rows)} rows in one call, and the limit is "
                    f"{MAX_TIER_PRICES}. Split the paste.",
                    "bulk_limit",
                )
            }
        )


def _clean_amount(amount, field: str) -> Decimal:
    """The two refusals every tier amount gets, wherever it was typed.

    Named once because the single create, the update and the 300-row paste all
    mean the same thing by "a price", and three copies of a money rule is three
    chances for one of them to drift a cent.

    The decimal rule asks what would be CHARGED, not how many characters were
    typed. Counting the exponent refused every stored price the API had just
    handed back: the column holds three places, so an untouched 270.00 comes out
    as "270.000", the grid re-sends it on a QUANTITY edit, and the merchant was
    told their own price had too many decimals. "270.000" and "119.990" are
    270.00 and 119.99 to the cent, so they are the price. "119.995" is not: it
    rounds to 120.00 and the half cent is real precision the merchant meant and
    nobody can pay. `to_money` is the fork's one rounding rule
    (`saleor/wsm/money.py`, ROUND_HALF_UP), the same one the checkout charges
    with, so the comparison here and the charge there cannot drift.

    What is RETURNED is the quantized value, so what gets stored is what would
    be charged rather than the third place the caller happened to send.
    """
    if amount is None:
        raise ValidationError({field: error("say what this group pays", "required")})
    amount = Decimal(amount)
    charged = to_money(amount)
    if charged != amount:
        raise ValidationError(
            {
                field: error(
                    "a price has at most two decimal places",
                    "tier_amount_too_many_decimals",
                )
            }
        )
    amount = charged
    if amount < models.MIN_TIER_AMOUNT:
        raise ValidationError(
            {
                field: error(
                    "a dealer price is a price, so it is at least one cent",
                    "tier_amount_below_one_cent",
                )
            }
        )
    return amount


# --- groups ------------------------------------------------------------------


class WsmDealerGroupCreateInput(BaseInputObjectType):
    code = graphene.String(
        required=True,
        description=(
            "The exact code that names this group everywhere else. Never "
            "changed once prices point at it."
        ),
    )
    name = graphene.String(description="What staff see. Blank shows the code.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmDealerGroupUpdateInput(BaseInputObjectType):
    code = graphene.String(description="Changing this re-points every compose delta.")
    name = graphene.String(description="What staff see. Blank shows the code.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class GroupWriteMixin:
    @classmethod
    def clean_input(cls, info, instance, data, **kwargs):
        cleaned_input = super().clean_input(info, instance, data, **kwargs)
        code = (cleaned_input.get("code") or "").strip()
        if "code" in cleaned_input:
            if not code:
                raise ValidationError(
                    {"code": error("a group needs a code", "required")}
                )
            cleaned_input["code"] = code
            # Its own check rather than `full_clean`'s, which calls this
            # "unique": the Dashboard renders one sentence per code, and
            # "unique" is the code it would also get from a duplicate anything.
            taken = (
                models.DealerGroup.objects.using(
                    get_database_connection_name(info.context)
                )
                .filter(code=code)
                .exclude(pk=instance.pk)
                .exists()
            )
            if taken:
                raise ValidationError(
                    {
                        "code": error(
                            f"{code!r} already names a dealer group",
                            "duplicate_group_code",
                        )
                    }
                )
        return cleaned_input


class WsmDealerGroupCreate(GroupWriteMixin, DeprecatedModelMutation):
    class Arguments:
        input = WsmDealerGroupCreateInput(
            required=True, description="Fields required to create a dealer group."
        )

    class Meta:
        description = "Create a buyer group."
        model = models.DealerGroup
        object_type = WsmDealerGroup
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class WsmDealerGroupUpdate(GroupWriteMixin, DeprecatedModelMutation):
    class Arguments:
        id = graphene.ID(required=True, description="ID of the group to update.")
        input = WsmDealerGroupUpdateInput(
            required=True, description="Fields required to update a dealer group."
        )

    class Meta:
        description = "Update a buyer group."
        model = models.DealerGroup
        object_type = WsmDealerGroup
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class GroupDeleteMixin:
    """A group with customers in it is refused here, not by the database.

    `DealerCustomer.group` is `on_delete=PROTECT`, so the delete would raise
    `ProtectedError` out of the view: a 500 on a merchant screen where the
    honest answer is a sentence naming how many shoppers are in the way.
    """

    @classmethod
    def clean_instance(cls, info, instance, /):
        count = instance.customers.count()
        if count:
            raise ValidationError(
                {
                    "id": error(
                        f"{count} customer(s) buy at this group's prices. Move "
                        f"them to another group first.",
                        "group_in_use",
                    )
                }
            )

    @classmethod
    def clean_input(cls, info, instances, ids):
        """Name the ROW that was refused, and keep the code that says why.

        `BaseBulkMutation.clean_input` keys its error dict by the node's GLOBAL
        ID and joins the messages into a bare string, dropping the code on the
        way (`saleor/graphql/core/mutations.py:1066-1067`, where stock's own
        FIXME says so). The refusal therefore reached the Dashboard as
        `field: "V3NtRGVhbGVyR3JvdXA6MQ=="`, `code: INVALID`: the Dashboard
        shows a message as a FORM error only when `field` is empty and branches
        on the code for everything else, so GROUP_IN_USE was neither readable
        nor recognisable and the merchant saw base64. `ids.<i>` is how stock's
        bulk mutations name a row, and `get_nodes` returns the instances in the
        order the ids were posted (`saleor/graphql/utils/__init__.py:144`), so
        the index is the line the merchant selected.
        """
        clean_instance_ids: list = []
        errors_dict: dict[str, list[ValidationError]] = {}
        for index, instance in enumerate(instances):
            try:
                cls.clean_instance(info, instance)
            except ValidationError as exc:
                errors_dict[f"ids.{index}"] = (
                    [item for items in exc.error_dict.values() for item in items]
                    if hasattr(exc, "error_dict")
                    else list(exc.error_list)
                )
            else:
                clean_instance_ids.append(instance.pk)
        return clean_instance_ids, errors_dict


class WsmDealerGroupDelete(GroupDeleteMixin, ModelDeleteMutation):
    class Arguments:
        id = graphene.ID(required=True, description="ID of the group to delete.")

    class Meta:
        description = (
            "Delete a buyer group. Its tier prices go with it; a group with "
            "customers in it is refused."
        )
        model = models.DealerGroup
        object_type = WsmDealerGroup
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class WsmDealerGroupBulkDelete(GroupDeleteMixin, ModelBulkDeleteMutation):
    class Arguments:
        ids = NonNullList(
            graphene.ID, required=True, description="IDs of the groups to delete."
        )

    class Meta:
        description = "Delete buyer groups. Any group with customers is skipped."
        model = models.DealerGroup
        object_type = WsmDealerGroup
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


# --- customers ---------------------------------------------------------------


class WsmDealerCustomerAssignInput(BaseInputObjectType):
    user = graphene.ID(required=True, description="The shopper's account.")
    group = graphene.ID(required=True, description="The group whose prices they get.")
    tax_exempt = graphene.Boolean(description="Charge this shopper no sales tax.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmDealerCustomerUpdateInput(BaseInputObjectType):
    group = graphene.ID(description="Move this shopper to another group.")
    tax_exempt = graphene.Boolean(description="Charge this shopper no sales tax.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmDealerCustomerAssign(TypedIdMixin, DeprecatedModelMutation):
    """Assign, not create, on stock's `giftCardAddNote` naming for the same shape.

    A shopper buys at one group's prices or none (`DealerCustomer.user` is a
    OneToOne), so a second assignment is a merchant looking at a stale list, not
    a merchant asking for two prices.
    """

    typed_ids = {"user": "User", "group": WsmDealerGroup}

    class Arguments:
        input = WsmDealerCustomerAssignInput(
            required=True, description="Fields required to assign a dealer customer."
        )

    class Meta:
        description = "Put a shopper in a buyer group."
        model = models.DealerCustomer
        object_type = WsmDealerCustomer
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    @classmethod
    def clean_input(cls, info, instance, data, **kwargs):
        cleaned_input = super().clean_input(info, instance, data, **kwargs)
        user = cleaned_input.get("user")
        if user is not None:
            existing = (
                models.DealerCustomer.objects.using(
                    get_database_connection_name(info.context)
                )
                .select_related("group")
                .filter(user=user)
                .first()
            )
            if existing is not None:
                raise ValidationError(
                    {
                        "user": error(
                            f"{user.email} already buys at {existing.group}'s "
                            f"prices. Move them instead.",
                            "customer_already_assigned",
                        )
                    }
                )
        return cleaned_input


class WsmDealerCustomerUpdate(TypedIdMixin, DeprecatedModelMutation):
    typed_ids = {"group": WsmDealerGroup}

    class Arguments:
        id = graphene.ID(required=True, description="ID of the assignment.")
        input = WsmDealerCustomerUpdateInput(
            required=True, description="Fields required to update a dealer customer."
        )

    class Meta:
        description = "Move a shopper to another group, or change their tax status."
        model = models.DealerCustomer
        object_type = WsmDealerCustomer
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class WsmDealerCustomerUnassign(ModelDeleteMutation):
    class Arguments:
        id = graphene.ID(required=True, description="ID of the assignment to remove.")

    class Meta:
        description = (
            "Take a shopper out of their buyer group. The account itself is "
            "untouched: this row is a link, never the customer."
        )
        model = models.DealerCustomer
        object_type = WsmDealerCustomer
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


# --- tier prices -------------------------------------------------------------


class WsmTierPriceCreateInput(BaseInputObjectType):
    variant = graphene.ID(required=True, description="The exact SKU.")
    group = graphene.ID(required=True, description="The group that pays this price.")
    min_quantity = graphene.Int(description="This price applies from here up.")
    amount = WsmDecimal(
        required=True, description="What the group pays each, at least one cent."
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmTierPriceUpdateInput(BaseInputObjectType):
    min_quantity = graphene.Int(description="This price applies from here up.")
    amount = WsmDecimal(description="What the group pays each.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class TierPriceWriteMixin(TypedIdMixin):
    typed_ids = {"variant": "ProductVariant", "group": WsmDealerGroup}

    @classmethod
    def clean_input(cls, info, instance, data, **kwargs):
        cleaned_input = super().clean_input(info, instance, data, **kwargs)
        if "amount" in cleaned_input:
            cleaned_input["amount"] = _clean_amount(cleaned_input["amount"], "amount")
        quantity = cleaned_input.get("min_quantity")
        if quantity is not None and quantity < 1:
            raise ValidationError(
                {"minQuantity": error("a quantity break starts at one", "invalid")}
            )
        return cleaned_input

    @classmethod
    def clean_instance(cls, info, instance, /):
        """The break this row would occupy, checked before it is called "unique".

        `wsm_dealer_one_row_per_break` is the constraint; the code a merchant
        screen renders for it is the one that says what a break IS, because
        "unique" on a screen with four fields names none of them.
        """
        taken = (
            models.TierPrice.objects.using(get_database_connection_name(info.context))
            .filter(
                variant_id=instance.variant_id,
                group_id=instance.group_id,
                min_quantity=instance.min_quantity,
            )
            .exclude(pk=instance.pk)
            .exists()
        )
        if taken:
            raise ValidationError(
                {
                    "minQuantity": error(
                        "this group already has a price for that SKU at that quantity",
                        "duplicate_tier_break",
                    )
                }
            )
        super().clean_instance(info, instance)


class WsmTierPriceCreate(TierPriceWriteMixin, DeprecatedModelMutation):
    class Arguments:
        input = WsmTierPriceCreateInput(
            required=True, description="Fields required to create a tier price."
        )

    class Meta:
        description = "Give one group one price for one SKU."
        model = models.TierPrice
        object_type = WsmTierPrice
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class WsmTierPriceUpdate(TierPriceWriteMixin, DeprecatedModelMutation):
    """The SKU and the group are not in the input, as they were not in the form.

    Moving a price to another SKU is not an edit of this row, it is a different
    price: the row a merchant is looking at is "what dealer-1 pays for this
    SKU", and a screen that let the SKU change under it would silently rewrite
    the wrong line.
    """

    class Arguments:
        id = graphene.ID(required=True, description="ID of the tier price.")
        input = WsmTierPriceUpdateInput(
            required=True, description="Fields required to update a tier price."
        )

    class Meta:
        description = "Change what a group pays, or from what quantity."
        model = models.TierPrice
        object_type = WsmTierPrice
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class WsmTierPriceDelete(ModelDeleteMutation):
    class Arguments:
        id = graphene.ID(required=True, description="ID of the tier price to delete.")

    class Meta:
        description = "Delete one quantity break. The SKU is untouched."
        model = models.TierPrice
        object_type = WsmTierPrice
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class WsmTierPriceBulkDelete(ModelBulkDeleteMutation):
    class Arguments:
        ids = NonNullList(
            graphene.ID, required=True, description="IDs of the tier prices."
        )

    class Meta:
        description = "Delete quantity breaks. The SKUs are untouched."
        model = models.TierPrice
        object_type = WsmTierPrice
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class WsmTierPriceBulkCreateInput(BaseInputObjectType):
    variant = graphene.ID(required=True, description="The exact SKU.")
    group = graphene.ID(required=True, description="The group that pays this price.")
    min_quantity = graphene.Int(description="This price applies from here up.")
    amount = WsmDecimal(
        required=True, description="What the group pays each, at least one cent."
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmTierPriceBulkCreate(BaseMutation):
    """The paste/import path: 300+ rows in one call, one transaction, one write.

    A loop of `wsmTierPriceCreate` would be 300 round trips and 1,200 queries to
    land one spreadsheet column, which is the shape live data actually has
    (304+ rows on one group, `dealer/admin.py:32`). This validates the whole
    payload against itself and against what is stored in a FIXED number of
    queries, then writes it with one `bulk_create`.

    `bulk_create` skips `full_clean`, which is the point and also the risk: the
    amount rule, the quantity rule and the break rule are all enforced here,
    above the same `_clean_amount` the single-row mutations call, and the
    `CheckConstraint` underneath is the backstop that turns a miss into a
    refused transaction rather than a charged cent.
    """

    count = graphene.Int(
        required=True, description="How many tier prices were created."
    )
    tier_prices = NonNullList(
        WsmTierPrice, required=True, description="The rows that were created."
    )

    class Arguments:
        tier_prices = NonNullList(
            WsmTierPriceBulkCreateInput,
            required=True,
            description="The rows to create.",
        )

    class Meta:
        description = "Create many tier prices in one call. One transaction."
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    @classmethod
    def perform_mutation(cls, _root, info, /, *, tier_prices, **data):
        try:
            rows = cls._clean_rows(info, tier_prices)
        except ValidationError as exc:
            # `count` and `tierPrices` are non-null, so a refusal has to carry
            # them: a payload that returned null for either would be a schema
            # error on top of the merchant's own.
            return cls.handle_errors(exc, count=0, tier_prices=[])
        with transaction.atomic():
            # The widest INSERT we will build in one round trip. Equal to the
            # cap today, so a legal paste is one statement; named separately
            # because raising the cap must not widen the statement.
            created = models.TierPrice.objects.bulk_create(rows, batch_size=500)
        return cls(errors=[], count=len(created), tier_prices=created)

    @classmethod
    def _clean_rows(cls, info, rows):
        """Every row checked against the payload and the table, in five queries.

        Errors name the row the way stock's bulk mutations do,
        `tierPrices.<index>.<field>`, so a failed paste of 300 lines tells the
        merchant which line to fix instead of which column.
        """
        from ....product.models import ProductVariant

        _check_bulk_limit(rows)

        database = get_database_connection_name(info.context)
        errors: dict = {}
        parsed: list[dict] = []
        seen: dict = {}

        for index, row in enumerate(rows):
            field = f"tierPrices.{index}"
            variant_pk = pk_or_none(row["variant"], "ProductVariant")
            group_pk = pk_or_none(row["group"], "WsmDealerGroup")
            if variant_pk is None:
                errors[f"{field}.variant"] = error(
                    "that is not a product variant", "invalid"
                )
                continue
            if group_pk is None:
                errors[f"{field}.group"] = error(
                    "that is not a dealer group", "invalid"
                )
                continue
            quantity = row.get("min_quantity")
            quantity = 1 if quantity is None else quantity
            if quantity < 1:
                errors[f"{field}.minQuantity"] = error(
                    "a quantity break starts at one", "invalid"
                )
                continue
            try:
                amount = _clean_amount(row.get("amount"), f"{field}.amount")
            except ValidationError as exc:
                errors.update(exc.error_dict)
                continue
            break_key = (variant_pk, group_pk, quantity)
            if break_key in seen:
                errors[f"{field}.minQuantity"] = error(
                    f"row {seen[break_key]} in this paste already prices that "
                    f"SKU for that group at that quantity",
                    "duplicate_tier_break",
                )
                continue
            seen[break_key] = index
            parsed.append(
                {
                    "index": index,
                    "variant_pk": variant_pk,
                    "group_pk": group_pk,
                    "min_quantity": quantity,
                    "amount": amount,
                }
            )

        if errors:
            raise ValidationError(errors)

        variant_pks = {row["variant_pk"] for row in parsed}
        group_pks = {row["group_pk"] for row in parsed}
        # The rows this mutation RETURNS are the rows the Dashboard renders,
        # and it selects `variant.product.name` and `currencyCode` on each one.
        # Bare instances make that a query per line of the paste the merchant
        # just made, which is the 1+N the bulk path exists to avoid.
        variants = (
            ProductVariant.objects.using(database)
            .select_related("product")
            .prefetch_related("channel_listings")
            .in_bulk(variant_pks)
        )
        groups = models.DealerGroup.objects.using(database).in_bulk(group_pks)
        stored = set(
            models.TierPrice.objects.using(database)
            .filter(variant_id__in=variant_pks, group_id__in=group_pks)
            .values_list("variant_id", "group_id", "min_quantity")
        )

        instances = []
        for row in parsed:
            field = f"tierPrices.{row['index']}"
            variant = variants.get(row["variant_pk"])
            group = groups.get(row["group_pk"])
            if variant is None:
                errors[f"{field}.variant"] = error(
                    "that SKU does not exist", "not_found"
                )
                continue
            if group is None:
                errors[f"{field}.group"] = error(
                    "that dealer group does not exist", "not_found"
                )
                continue
            if (variant.pk, group.pk, row["min_quantity"]) in stored:
                errors[f"{field}.minQuantity"] = error(
                    "this group already has a price for that SKU at that quantity",
                    "duplicate_tier_break",
                )
                continue
            instances.append(
                models.TierPrice(
                    variant=variant,
                    group=group,
                    min_quantity=row["min_quantity"],
                    amount=row["amount"],
                )
            )

        if errors:
            raise ValidationError(errors)
        return instances


class WsmTierPriceBulkUpdateInput(BaseInputObjectType):
    id = graphene.ID(required=True, description="ID of the tier price to change.")
    min_quantity = graphene.Int(description="This price applies from here up.")
    amount = WsmDecimal(description="What the group pays each.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmTierPriceBulkUpdate(BaseMutation):
    """The grid's save button: every edited row in one call, one UPDATE.

    `wsmTierPriceBulkCreate` gave the merchant a paste path and left them with
    no way OUT of it: a 300-row price list that needed a five percent cut was
    300 round trips through `wsmTierPriceUpdate`, each one a full mutation with
    its own validation queries, or a delete-and-repaste that loses every row's
    id. This is the same shape as the create: the whole payload is validated
    against itself and against the table in a FIXED number of queries, then
    written with one `bulk_update`.

    A field the caller does not send is a field left alone, which is what lets
    the grid post only the cells that changed. The row is loaded, the changes
    are applied to the loaded instance, and the break rule is then checked
    against what the row WOULD become, because moving two rows onto one
    quantity is the mistake a grid makes and `wsm_dealer_one_row_per_break`
    would answer it with a 500.
    """

    count = graphene.Int(
        required=True, description="How many tier prices were changed."
    )
    tier_prices = NonNullList(
        WsmTierPrice, required=True, description="The rows as they now stand."
    )

    class Arguments:
        tier_prices = NonNullList(
            WsmTierPriceBulkUpdateInput,
            required=True,
            description="The rows to change.",
        )

    class Meta:
        description = "Change many tier prices in one call. One transaction."
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    @classmethod
    def perform_mutation(cls, _root, info, /, *, tier_prices, **data):
        try:
            rows = cls._clean_rows(info, tier_prices)
        except ValidationError as exc:
            # `count` and `tierPrices` are non-null, so a refusal has to carry
            # them, exactly as the create path does.
            return cls.handle_errors(exc, count=0, tier_prices=[])
        with transaction.atomic():
            # One statement for the whole grid. `batch_size` equals the cap, so
            # a legal call is one round trip; raising the cap must not widen it.
            models.TierPrice.objects.bulk_update(
                rows, ["amount", "min_quantity"], batch_size=MAX_TIER_PRICES
            )
        return cls(errors=[], count=len(rows), tier_prices=rows)

    @classmethod
    def _clean_rows(cls, info, rows):
        """Every row checked against the payload and the table, in two queries.

        Errors name the row the way the create path does,
        `tierPrices.<index>.<field>`, because the Dashboard section maps that
        string to a grid CELL: a refusal with no index is a red banner over a
        300-row grid with nothing highlighted in it.
        """
        _check_bulk_limit(rows)

        database = get_database_connection_name(info.context)
        errors: dict = {}
        parsed: list[dict] = []

        for index, row in enumerate(rows):
            field = f"tierPrices.{index}"
            pk = pk_or_none(row["id"], "WsmTierPrice")
            if pk is None:
                errors[f"{field}.id"] = error("that is not a tier price", "invalid")
                continue
            quantity = row.get("min_quantity")
            if quantity is not None and quantity < 1:
                errors[f"{field}.minQuantity"] = error(
                    "a quantity break starts at one", "invalid"
                )
                continue
            amount = row.get("amount")
            if amount is not None:
                try:
                    amount = _clean_amount(amount, f"{field}.amount")
                except ValidationError as exc:
                    errors.update(exc.error_dict)
                    continue
            parsed.append(
                {
                    "index": index,
                    "pk": pk,
                    "min_quantity": quantity,
                    "amount": amount,
                }
            )

        if errors:
            raise ValidationError(errors)

        # The rows this mutation RETURNS are the rows the grid re-renders, and
        # it selects `variant.product.name` and `currencyCode` on each one, so
        # they are loaded the way every other tier-price read is.
        stored = (
            models.TierPrice.objects.using(database)
            .select_related("variant", "variant__product", "group")
            .prefetch_related("variant__channel_listings")
            .in_bulk([row["pk"] for row in parsed])
        )

        instances = []
        index_of: dict[int, int] = {}
        wanted: dict[tuple, int] = {}
        for row in parsed:
            field = f"tierPrices.{row['index']}"
            instance = stored.get(row["pk"])
            if instance is None:
                errors[f"{field}.id"] = error(
                    "that tier price does not exist", "not_found"
                )
                continue
            if row["amount"] is not None:
                instance.amount = row["amount"]
            if row["min_quantity"] is not None:
                instance.min_quantity = row["min_quantity"]
            break_key = (instance.variant_id, instance.group_id, instance.min_quantity)
            if break_key in wanted:
                errors[f"{field}.minQuantity"] = error(
                    f"row {wanted[break_key]} in this call already prices that "
                    f"SKU for that group at that quantity",
                    "duplicate_tier_break",
                )
                continue
            wanted[break_key] = row["index"]
            index_of[instance.pk] = row["index"]
            instances.append(instance)

        if errors:
            raise ValidationError(errors)

        # The breaks that belong to rows this call is NOT touching. One query,
        # whatever the row count, and `exclude` is what keeps a row from
        # colliding with the version of itself still in the table.
        taken = set(
            models.TierPrice.objects.using(database)
            .filter(
                variant_id__in={instance.variant_id for instance in instances},
                group_id__in={instance.group_id for instance in instances},
            )
            .exclude(pk__in=list(stored))
            .values_list("variant_id", "group_id", "min_quantity")
        )
        for instance in instances:
            key = (instance.variant_id, instance.group_id, instance.min_quantity)
            if key in taken:
                errors[f"tierPrices.{index_of[instance.pk]}.minQuantity"] = error(
                    "this group already has a price for that SKU at that quantity",
                    "duplicate_tier_break",
                )

        if errors:
            raise ValidationError(errors)
        return instances


# --- settings ----------------------------------------------------------------


class WsmDealerSettingsInput(BaseInputObjectType):
    discount_stacking = graphene.Boolean(
        required=True,
        description=(
            "False: a dealer-priced line takes no further discount. True: "
            "discounts combine with dealer prices."
        ),
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmDealerSettingsUpdate(DeprecatedModelMutation):
    """The singleton update.

    `DeprecatedModelMutation` is what stock's own model mutations still use
    (`saleor/graphql/giftcard/mutations/gift_card_create.py`); there is no
    non-deprecated `ModelMutation` in 3.23 to match instead.
    """

    class Arguments:
        input = WsmDealerSettingsInput(
            required=True, description="Fields required to update dealer settings."
        )

    class Meta:
        description = "Update the store-wide dealer pricing settings."
        model = models.DealerSettings
        object_type = WsmDealerSettings
        permissions = DEALER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    @classmethod
    def get_instance(cls, info, **data):
        """One row, at one key, whoever is saving.

        The inherited implementation reads `data["id"]` and, finding none,
        returns `model()`: a FRESH instance, which on save would give this
        singleton table a second row on every call. There is no id to take,
        because the row is the store.

        `.first() or DealerSettings()` fixed the sequential case and left the
        concurrent one: two merchants saving the settings screen at the same
        moment both read no row and both INSERT one, and the toggle they are
        writing decides whether a voucher stacks on a dealer price. Naming the
        KEY makes the race unwritable, and `wsm_dealer_settings_is_one_row`
        (migration 0002) is the backstop under it for every other writer.
        """
        instance, _created = models.DealerSettings.objects.using(
            get_database_connection_name(info.context)
        ).get_or_create(pk=1)
        return instance
