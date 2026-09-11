# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Compose's eight single-row mutations, and the nested lists two of them carry.

Every rule enforced here is already a rule on a model in `saleor/wsm/compose/`.
Nothing new is invented: the mutation's whole job is to reach the same `clean()`
the Django admin reached, with the same WHOLE-SUBMIT picture the inline formset
gave it (`compose/forms.py:236`), because a single-row check compares a row
being fixed against siblings still stored broken and refuses the very submit
that fixes them.

How the whole-submit picture is taken, and why it is taken this way: the nested
rows are SAVED first and validated after, inside one `transaction.atomic()`, so
the floor rule reads the edit exactly as it would stand rather than a hand-built
model of it. A failure raises, the block unwinds, and nothing is left behind.
The alternative, threading `pending_values` / `removed_value_pks` through every
check, is the same answer for more code and one more thing to keep in step with
the admin.

What makes that order safe is that no `post_save` receiver is registered on any
of these tables: a receiver would OBSERVE rows the failing check then unwinds,
and a receiver that writes outside the transaction (a webhook, a search push)
would act on an edit that never happened. The first one added has to move the
validation ahead of the save, and this paragraph is the note that says so.
"""

import graphene
from django.core.exceptions import ValidationError
from django.db import transaction

from ....graphql.core.mutations import DeprecatedModelMutation, ModelDeleteMutation
from ....graphql.core.types import BaseInputObjectType, NonNullList
from ....graphql.core.utils import from_global_id_or_error
from ....graphql.product.types import Product
from ....permission.enums import ProductPermissions
from ...compose import models
from ..dealer.mutations import DEALER_PERMISSIONS
from ..errors import WsmError
from ..scalars import WsmDecimal
from ..types import DOC_CATEGORY_WSM
from ..utils import TypedIdMixin
from .enums import WsmFeeBasisEnum, WsmFeeScopeEnum, WsmOptionSetPromptTypeEnum
from .types import (
    WsmDealerTierOptionPrice,
    WsmFee,
    WsmOptionSet,
    WsmOptionValue,
    WsmProductCompliance,
)

MANAGE_PRODUCTS = (ProductPermissions.MANAGE_PRODUCTS,)


def _row_pk(raw_id, only_type, field):
    """A child row's global ID as an integer, or a NOT_FOUND field error."""
    try:
        _type, pk = from_global_id_or_error(raw_id, only_type, raise_error=True)
        return int(pk)
    except Exception:
        raise ValidationError(
            {
                field: ValidationError(
                    f"{raw_id!r} is not the ID of a {only_type.__name__} row.",
                    code="not_found",
                )
            }
        ) from None


def _not_found(field, message):
    return ValidationError({field: ValidationError(message, code="not_found")})


def _clean_row(row, field_prefix, field_map):
    """`full_clean` one child row, with its errors named by their INPUT field.

    Django names an error after the model column; the Dashboard is rendering an
    input called `values.2.skuFragment`. Without this remap a merchant gets a
    correct sentence attached to a field that is not on their screen.
    """
    try:
        row.full_clean()
    except ValidationError as error:
        remapped = {}
        for column, errors in error.error_dict.items():
            name = field_map.get(column, column)
            remapped[f"{field_prefix}.{name}" if name else field_prefix] = errors
        raise ValidationError(remapped) from None


VALUE_FIELDS = {
    "name": "name",
    "sku_fragment": "skuFragment",
    "price_delta": "priceDelta",
    "image_url": "imageUrl",
    "sort_order": "sortOrder",
    "option_set": "",
    "__all__": "",
}
TIER_FIELDS = {"tier_group": "tierGroup", "price_delta": "priceDelta", "__all__": ""}


def replace_tier_deltas(value_row, tier_input, field_prefix):
    """Replace one choice's dealer rows: provided means the whole list.

    `DUPLICATE_TIER_GROUP` is checked here rather than left to the unique
    constraint, because the constraint fires in the database as an
    IntegrityError once the first duplicate is written, which is a 500 and not a
    field error. The admin's formset said the same sentence for the same reason
    (`compose/forms.py:177`).
    """
    stored = {row.pk: row for row in value_row.tier_deltas.all()}
    kept, rows = [], []
    seen: dict[str, int] = {}
    for index, item in enumerate(tier_input):
        field = f"{field_prefix}.tierDeltas.{index}"
        if item.get("id"):
            pk = _row_pk(item["id"], WsmDealerTierOptionPrice, f"{field}.id")
            row = stored.get(pk)
            if row is None:
                raise _not_found(
                    f"{field}.id",
                    "That dealer price is not on this choice, so there is "
                    "nothing here to edit.",
                )
            kept.append(pk)
        else:
            row = models.DealerTierOptionPrice(option_value=value_row)
        row.tier_group = item["tier_group"]
        row.price_delta = item["price_delta"]
        group = row.tier_group
        if group in seen:
            raise ValidationError(
                {
                    f"{field}.tierGroup": ValidationError(
                        "This choice already has a price for that dealer group. "
                        "Change the group, or edit the row that already has it.",
                        code=models.DUPLICATE_TIER_GROUP,
                    )
                }
            )
        seen[group] = index
        rows.append((row, field))

    # ponytail: rows are deleted first and saved after, so two rows SWAPPING
    # dealer groups inside one submit transiently collide on
    # (option_value, tier_group) and the save refuses. Ceiling: a merchant who
    # swaps two groups between existing rows has to delete and re-add instead.
    # Upgrade path is the same one containers' `_write_members` carries, a
    # deferrable unique constraint; the Dashboard datagrid deletes and adds, so
    # nothing reaches it today.
    value_row.tier_deltas.exclude(pk__in=kept).delete()
    for row, _field in rows:
        row.save()
    # Validated only once every row is stored, so a group whose credit is deeper
    # than retail's is measured against the edit as it stands and not against a
    # half-applied version of it.
    for row, field in rows:
        _clean_row(row, field, TIER_FIELDS)


def replace_option_values(option_set, values_input):
    """Replace a question's whole answer list, then check it as one submit."""
    stored = {row.pk: row for row in option_set.values.all()}
    kept, rows = [], []
    for index, item in enumerate(values_input):
        field = f"values.{index}"
        if item.get("id"):
            pk = _row_pk(item["id"], WsmOptionValue, f"{field}.id")
            row = stored.get(pk)
            if row is None:
                raise _not_found(
                    f"{field}.id",
                    "That choice is not on this question, so there is nothing "
                    "here to edit.",
                )
            kept.append(pk)
        else:
            row = models.OptionValue(option_set=option_set)
        row.name = item["name"]
        # `is not None`, like the three fields below it: the Dashboard
        # datagrid sends only what the merchant touched, so an absent fragment
        # means unchanged. Reading it as a clear renames every SKU the choice
        # builds, on an edit that never mentioned it.
        if item.get("sku_fragment") is not None:
            row.sku_fragment = item["sku_fragment"]
        if item.get("price_delta") is not None:
            row.price_delta = item["price_delta"]
        if item.get("image_url") is not None:
            row.image_url = item["image_url"]
        if item.get("sort_order") is not None:
            row.sort_order = item["sort_order"]
        # The two rules a single row cannot see are checked across the whole
        # list below, exactly as the inline formset checked them.
        row.floor_checked_by_formset = True
        rows.append((row, field, item.get("tier_deltas")))

    option_set.values.exclude(pk__in=kept).delete()
    for row, field, _tiers in rows:
        row.option_set = option_set
        _clean_row(row, field, VALUE_FIELDS)
        row.save()

    seen: dict[str, str] = {}
    for row, field, _tiers in rows:
        if not row.sku_fragment:
            continue
        if row.sku_fragment in seen:
            raise ValidationError(
                {
                    f"{field}.skuFragment": models.duplicate_fragment_error(
                        seen[row.sku_fragment], row.sku_fragment
                    )
                }
            )
        seen[row.sku_fragment] = row.name

    for row, field, tiers in rows:
        if tiers is not None:
            replace_tier_deltas(row, tiers, field)

    check_configured_floor(option_set.product_id)


def check_configured_floor(product_id):
    """The floor rule, asked of the edit as it now stands, retail then dealer.

    Both halves, because the retail floor is only an UPPER BOUND on a dealer's:
    a group whose credits are deeper goes under first, and this submit is the
    merchant's last chance to hear about it before that group's add-to-cart
    starts refusing.
    """
    floor, base = models.configured_floor_cents(product_id)
    if floor is not None and floor <= 0:
        raise ValidationError({"values": models.floor_error(floor, base)})
    problem = models.dealer_floor_problem(product_id)
    if problem is not None:
        raise ValidationError({"values": problem})


class WsmDealerTierOptionPriceInput(BaseInputObjectType):
    id = graphene.ID(description="Omit to create. Supply to edit the row in place.")
    tier_group = graphene.String(
        required=True, description="A DealerGroup code, not a global ID."
    )
    price_delta = WsmDecimal(
        required=True, description="Signed. Never above the retail delta."
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmOptionValueInput(BaseInputObjectType):
    id = graphene.ID(description="Omit to create. Supply to edit the row in place.")
    name = graphene.String(required=True, description="What the shopper sees.")
    sku_fragment = graphene.String(description="Appended to the product SKU.")
    price_delta = WsmDecimal(description="Signed: a credit subtracts.")
    image_url = graphene.String(description="Swatch or thumbnail URL.")
    sort_order = graphene.Int(description="Low numbers first.")
    tier_deltas = NonNullList(
        WsmDealerTierOptionPriceInput,
        description=(
            "Omitted leaves this choice's dealer rows untouched. Provided "
            "replaces them."
        ),
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmOptionSetCreateInput(BaseInputObjectType):
    product = graphene.ID(required=True, description="The product asking the question.")
    name = graphene.String(required=True, description="The merchant's internal name.")
    label = graphene.String(description="What the shopper sees above the choices.")
    prompt_type = WsmOptionSetPromptTypeEnum(description="How the shopper answers.")
    required = graphene.Boolean(description="The shopper cannot decline this question.")
    note = graphene.String(description="Help shown under the question.")
    sort_order = graphene.Int(description="Low numbers first.")
    values = NonNullList(
        WsmOptionValueInput,
        description="Omit to create the question with no answers.",
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmOptionSetUpdateInput(BaseInputObjectType):
    name = graphene.String(description="The merchant's internal name.")
    label = graphene.String(description="What the shopper sees above the choices.")
    prompt_type = WsmOptionSetPromptTypeEnum(description="How the shopper answers.")
    required = graphene.Boolean(description="The shopper cannot decline this question.")
    note = graphene.String(description="Help shown under the question.")
    sort_order = graphene.Int(description="Low numbers first.")
    values = NonNullList(
        WsmOptionValueInput,
        description=(
            "Omitted leaves the answers untouched. Provided REPLACES the whole "
            "list, and the floor is checked across the whole list at once."
        ),
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmOptionSetMutationBase(TypedIdMixin, DeprecatedModelMutation):
    """What create and update share: the nested list, inside one transaction."""

    typed_ids = {"product": Product}

    class Meta:
        abstract = True

    @classmethod
    def clean_input(cls, info, instance, data, **kwargs):
        cleaned_input = super().clean_input(info, instance, data, **kwargs)
        cls.check_tier_delta_permission(info, cleaned_input.get("values"))
        return cleaned_input

    @classmethod
    def check_tier_delta_permission(cls, info, values):
        """`tierDeltas` is dealer money, so it asks for the dealer permission.

        MANAGE_PRODUCTS gates this mutation because a question on a product is
        catalog work. A tier delta is not: it writes `DealerTierOptionPrice`,
        what a named buyer group PAYS, and every mutation in
        `wsm/graphql/dealer` gates that on MANAGE_DISCOUNTS. Without this a
        catalog manager sets dealer prices through the option-set screen, which
        `dealer/schema.py` promises in writing they cannot.

        Asked of the INPUT rather than of what changed, because an empty list
        is a write too: provided REPLACES, so `tierDeltas: []` deletes every
        dealer price on that choice. Omitted leaves them untouched and stays a
        MANAGE_PRODUCTS-only save, which is the whole option-set screen for a
        store that sells to nobody at a negotiated price.

        A field error and not a top-level `PermissionDenied`, because the
        caller may run this mutation: the refusal belongs on the column the
        Dashboard renders, beside the save it is refusing.
        """
        for index, item in enumerate(values or []):
            if item.get("tier_deltas") is None:
                continue
            if cls.check_permissions(info.context, DEALER_PERMISSIONS):
                return
            raise ValidationError(
                {
                    f"values.{index}.tierDeltas": ValidationError(
                        "Dealer prices need the dealer-pricing permission. "
                        "Save the question without its dealer columns, or ask "
                        "someone who has it.",
                        code="permission_denied",
                    )
                }
            )

    @classmethod
    def construct_instance(cls, instance, cleaned_data):
        instance = super().construct_instance(instance, cleaned_data)
        # `values` provided means this submit owns the whole answer list, so the
        # set-wide check in `replace_option_values` is the right one, and the
        # model's row-wise copy of it would refuse an edit that fixes exactly
        # what it complains about. Carried on the INSTANCE rather than on the
        # mutation class, because a class attribute is shared by every request
        # this worker is serving.
        instance.floor_checked_by_formset = cleaned_data.get("values") is not None
        return instance

    @classmethod
    def post_save_action(cls, info, instance, cleaned_input):
        values = cleaned_input.get("values")
        if values is None:
            return
        replace_option_values(instance, values)

    @classmethod
    def perform_mutation(cls, root, info, /, **data):
        with transaction.atomic():
            return super().perform_mutation(root, info, **data)


class WsmOptionSetCreate(WsmOptionSetMutationBase):
    class Meta:
        description = "Create a question on a product."
        model = models.OptionSet
        object_type = WsmOptionSet
        return_field_name = "optionSet"
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    class Arguments:
        input = WsmOptionSetCreateInput(
            required=True, description="Fields required to create an option set."
        )


class WsmOptionSetUpdate(WsmOptionSetMutationBase):
    class Meta:
        description = "Update a question and, optionally, its whole answer list."
        model = models.OptionSet
        object_type = WsmOptionSet
        return_field_name = "optionSet"
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    class Arguments:
        id = graphene.ID(required=True, description="ID of the option set to update.")
        input = WsmOptionSetUpdateInput(
            required=True, description="Fields to update on the option set."
        )


class WsmOptionSetDelete(ModelDeleteMutation):
    class Meta:
        description = "Delete a question and every answer on it."
        model = models.OptionSet
        object_type = WsmOptionSet
        return_field_name = "optionSet"
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    class Arguments:
        id = graphene.ID(required=True, description="ID of the option set to delete.")


class WsmFeeCreateInput(BaseInputObjectType):
    product = graphene.ID(required=True, description="The product being charged.")
    label = graphene.String(required=True, description="What the shopper sees.")
    sku = graphene.String(description="The merchant's own code for the charge.")
    basis = WsmFeeBasisEnum(description="Flat amount or percentage.")
    amount = WsmDecimal(required=True, description="A flat charge, or the percentage.")
    apply_to = WsmFeeScopeEnum(description="Per item, or once per line.")
    required = graphene.Boolean(description="Always charged.")
    decline_label = graphene.String(description="Wording of the decline option.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmFeeUpdateInput(BaseInputObjectType):
    label = graphene.String(description="What the shopper sees.")
    sku = graphene.String(description="The merchant's own code for the charge.")
    basis = WsmFeeBasisEnum(description="Flat amount or percentage.")
    amount = WsmDecimal(description="A flat charge, or the percentage.")
    apply_to = WsmFeeScopeEnum(description="Per item, or once per line.")
    required = graphene.Boolean(description="Always charged.")
    decline_label = graphene.String(description="Wording of the decline option.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmFeeCreate(TypedIdMixin, DeprecatedModelMutation):
    """`variant` is deliberately absent from the input.

    The hidden carrier variant is written by the first configured add and never
    by hand (`Fee.ensure_variant`); a merchant who could set it can only point a
    charge at the wrong catalog row.
    """

    class Meta:
        description = "Attach a charge to a product."
        model = models.Fee
        object_type = WsmFee
        return_field_name = "fee"
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    typed_ids = {"product": Product}

    class Arguments:
        input = WsmFeeCreateInput(
            required=True, description="Fields required to create a charge."
        )


class WsmFeeUpdate(DeprecatedModelMutation):
    class Meta:
        description = "Update a charge."
        model = models.Fee
        object_type = WsmFee
        return_field_name = "fee"
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    class Arguments:
        id = graphene.ID(required=True, description="ID of the charge to update.")
        input = WsmFeeUpdateInput(
            required=True, description="Fields to update on the charge."
        )


class WsmFeeDelete(ModelDeleteMutation):
    class Meta:
        description = "Delete a charge."
        model = models.Fee
        object_type = WsmFee
        return_field_name = "fee"
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    class Arguments:
        id = graphene.ID(required=True, description="ID of the charge to delete.")


class WsmProductComplianceInput(BaseInputObjectType):
    prop65 = graphene.Boolean(description="Show the California Proposition 65 warning.")
    prop65_text = graphene.String(description="Your own Prop 65 wording.")
    restricted_states = NonNullList(
        graphene.String,
        description="Two-letter US codes. Provided replaces the whole list.",
    )
    include_shipping_zones = NonNullList(
        graphene.ID,
        description=(
            "Shipping zone IDs. Provided replaces the whole set. Empty list clears it."
        ),
    )
    restriction_message = graphene.String(
        description="What the shopper is told when a destination is refused."
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmProductComplianceUpdate(DeprecatedModelMutation):
    """Create-or-update on a OneToOne product row. There is no separate create.

    Keyed by the product rather than by row id because the model is a
    OneToOneField: there is at most one row, the merchant reaches it from the
    product, and a create mutation would only exist to be called once.
    """

    class Meta:
        description = "Set what a product must say and where it may not go."
        model = models.ProductCompliance
        object_type = WsmProductCompliance
        return_field_name = "compliance"
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    class Arguments:
        product = graphene.ID(required=True, description="ID of the product.")
        input = WsmProductComplianceInput(
            required=True, description="Fields to set on the compliance row."
        )

    @classmethod
    def get_instance(cls, info, **data):
        from ....graphql.product.types import Product

        product = cls.get_node_or_error(
            info, data["product"], only_type=Product, field="product"
        )
        return models.ProductCompliance.objects.filter(
            product_id=product.pk
        ).first() or models.ProductCompliance(product=product)

    @classmethod
    def clean_input(cls, info, instance, data, **kwargs):
        cleaned = super().clean_input(info, instance, data, **kwargs)
        if "restricted_states" in cleaned and cleaned["restricted_states"] is not None:
            # One comma-separated column on the model, a list on the wire. The
            # model's own `clean()` normalises and rejects what is not a US
            # subdivision code, so the join is all that belongs here.
            cleaned["restricted_states"] = ", ".join(cleaned["restricted_states"])
        return cleaned

    @classmethod
    def perform_mutation(cls, root, info, /, **data):
        with transaction.atomic():
            return super().perform_mutation(root, info, **data)


class WsmProductComplianceDelete(ModelDeleteMutation):
    class Meta:
        description = "Delete a product's compliance row."
        model = models.ProductCompliance
        object_type = WsmProductCompliance
        return_field_name = "compliance"
        permissions = MANAGE_PRODUCTS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    class Arguments:
        id = graphene.ID(
            required=True, description="ID of the compliance row to delete."
        )
