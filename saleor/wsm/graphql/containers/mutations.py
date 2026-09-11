# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The five container mutations. Two doors on a series, three on a kit.

Members and rules have no top-level mutations, for the reason the admin had no
standalone screens for them: a rule is about the parts beside it, so the whole
set is what has to be checked at once, and a mutation that took one row at a
time would refuse the very submit that fixes the set. Omitted leaves the list
alone; provided REPLACES it, rows absent from it are deleted; a child with `id`
is edited in place and one without is created.

Rules can point at members that do not exist yet, because a create lands the kit
and its parts in one call: `key` is a client-chosen handle for one mutation and
is never stored. Everything a rule names is resolved to a member SLOT during
validation and materialised after the members are written, so a rule can never
reference a row that the same payload was about to delete.

`RULE_TARGET_NOT_IN_KIT` is the one rule here that nothing enforced before. The
admin scoped the target picker to this kit's own members
(`containers/admin.py:243`) and the model never looked, because `targets` is an
m2m written after `clean()` has run. This is the first place it is checked.
"""

import graphene
from django.core.exceptions import ValidationError
from django.db import transaction

from ....attribute import AttributeType
from ....graphql.core.mutations import DeprecatedModelMutation, ModelDeleteMutation
from ....graphql.core.scalars import PositiveDecimal
from ....graphql.core.types import BaseInputObjectType, NonNullList
from ....graphql.core.utils import from_global_id_or_error
from ....permission.enums import ProductPermissions
from ...containers import models
from ..errors import WsmError
from ..types import DOC_CATEGORY_WSM
from .types import (
    WsmKitConfig,
    WsmKitDiscountKind,
    WsmKitRuleKind,
    WsmSeriesConfig,
)

# ponytail: every mutation here reuses MANAGE_PRODUCTS rather than adding a
# `WsmPermissions` enum, because that enum lives in a core file and our
# codenames would have to reach graphene's `PermissionEnum` to be assignable in
# the Dashboard's own permission-group screens. Ceiling: a staffer who can edit
# a product can edit a kit. Upgrade path: append a `WsmPermissions` class to
# `PERMISSIONS_ENUMS` from `ready()` before the schema is built, at which point
# the existing `create_wsm_permissions` receiver already carries the rows.
CONTAINER_PERMISSIONS = (ProductPermissions.MANAGE_PRODUCTS,)


def _error(message: str, code: str) -> ValidationError:
    return ValidationError(message, code=code)


def _pk_or_none(global_id, type_name: str):
    """The database id behind a global id, or None if it is not one of those."""
    try:
        _, pk = from_global_id_or_error(global_id, type_name, raise_error=True)
        return int(pk)
    except Exception:
        return None


def _collection_or_error(database: str, global_id, field: str = "collection"):
    from ....product.models import Collection

    pk = _pk_or_none(global_id, "Collection")
    collection = (
        Collection.objects.using(database).filter(pk=pk).first()
        if pk is not None
        else None
    )
    if collection is None:
        raise ValidationError(
            {field: _error("that collection does not exist", "not_found")}
        )
    return collection


# --- series ------------------------------------------------------------------


class WsmSeriesConfigInput(BaseInputObjectType):
    brand = graphene.String(description="The one brand this series covers.")
    axes = NonNullList(
        graphene.String,
        description=(
            "Attribute slugs, in the order the configurator asks them. "
            "Provided replaces the whole list."
        ),
    )
    partitioning_axis = graphene.String(
        description="The one axis that decides which product. One of `axes`."
    )
    miss_message = graphene.String(
        description="What a shopper is told when their answers match nothing."
    )
    published = graphene.Boolean(description="Show this series on the storefront.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmSeriesConfigUpdate(DeprecatedModelMutation):
    """Create-or-update, keyed by the collection, because the row is a OneToOne.

    There is no create mutation and no id argument for the same reason
    `wsmDealerSettingsUpdate` has none: the collection IS the key, so a screen
    that opened on a collection with no row yet would otherwise have to guess
    which of two mutations to send.
    """

    class Arguments:
        collection = graphene.ID(
            required=True, description="The collection this series configures."
        )
        input = WsmSeriesConfigInput(
            required=True, description="Fields required to update the series."
        )

    class Meta:
        description = (
            "Create or update the series on a collection. Restamps the "
            "collection's `wsm.series` metadata in the same transaction."
        )
        model = models.SeriesConfig
        object_type = WsmSeriesConfig
        return_field_name = "series"
        permissions = CONTAINER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM

    @classmethod
    def get_instance(cls, info, **data):
        from ....graphql.core.context import get_database_connection_name

        database = get_database_connection_name(info.context)
        collection = _collection_or_error(database, data["collection"])
        return models.SeriesConfig.objects.using(database).filter(
            collection=collection
        ).first() or models.SeriesConfig(collection=collection)

    @classmethod
    def clean_input(cls, info, instance, data, **kwargs):
        cleaned_input = super().clean_input(info, instance, data, **kwargs)
        cls._clean_axis_slugs(info, instance, cleaned_input)
        return cleaned_input

    @classmethod
    def _clean_axis_slugs(cls, info, instance, cleaned_input):
        """An axis the store has no attribute for asks a question nothing answers.

        This is the admin form's rule (`containers/admin.py:56`, `:108`): the
        screen offered the store's own product attributes, so a slug outside
        them could only be typed by a merchant who had gone to another
        application to look one up. A slug the row ALREADY holds is exempt,
        which is the admin's `(missing)` marker as a rule rather than a label: a
        store that deletes an attribute must not have the next save of an
        unrelated field silently drop a configurator question.
        """
        from ....attribute.models import Attribute
        from ....graphql.core.context import get_database_connection_name

        proposed = {
            slug: field
            for field, slug in (
                ("partitioningAxis", cleaned_input.get("partitioning_axis")),
            )
            if slug
        }
        for slug in cleaned_input.get("axes") or []:
            proposed.setdefault(slug, "axes")

        held = set(instance.axes or [])
        if instance.partitioning_axis:
            held.add(instance.partitioning_axis)
        unknown = set(proposed) - held
        if not unknown:
            return

        known = set(
            Attribute.objects.using(get_database_connection_name(info.context))
            .filter(type=AttributeType.PRODUCT_TYPE, slug__in=unknown)
            .values_list("slug", flat=True)
        )
        missing = sorted(unknown - known)
        if not missing:
            return
        raise ValidationError(
            {
                proposed[missing[0]]: _error(
                    f"the store has no product attribute for {missing}",
                    "unknown_attribute_slug",
                )
            }
        )


class WsmSeriesConfigDelete(ModelDeleteMutation):
    """Deleting the editor's row deletes what it published.

    `SeriesConfig.delete` clears `wsm.series` off the Collection in the same
    transaction, so this mutation adds nothing to it: the blob is derived output
    of the row (Dana, 2026-09-09), and a second place that knew to clear it
    would be a second authority.
    """

    class Arguments:
        id = graphene.ID(required=True, description="ID of the series to delete.")

    class Meta:
        description = (
            "Delete a series. Clears `wsm.series` from the collection's "
            "metadata in the same transaction."
        )
        model = models.SeriesConfig
        object_type = WsmSeriesConfig
        return_field_name = "series"
        permissions = CONTAINER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


# --- kits --------------------------------------------------------------------


class WsmKitMemberInput(BaseInputObjectType):
    id = graphene.ID(description="Omit to create. Supply to edit the row in place.")
    key = graphene.String(
        description=(
            "A client-chosen handle, unique within this one mutation, so a rule "
            "in the same payload can point at a member that has no ID yet. "
            "Never stored."
        )
    )
    variant = graphene.ID(required=True, description="The SKU this kit contains.")
    quantity = graphene.Int(description="How many of this SKU one kit contains.")
    sort_order = graphene.Int(description="Lowest first.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmKitMemberRuleInput(BaseInputObjectType):
    id = graphene.ID(description="Omit to create. Supply to edit the row in place.")
    subject_id = graphene.ID(description="Exactly one of subjectId or subjectKey.")
    subject_key = graphene.String(description="Exactly one of subjectId or subjectKey.")
    kind = WsmKitRuleKind(
        required=True, description="Needs one of, or cannot be sold with."
    )
    target_ids = NonNullList(
        graphene.ID, description="Existing members. Combined with targetKeys."
    )
    target_keys = NonNullList(
        graphene.String, description="Members created in this same payload."
    )
    message = graphene.String(
        required=True, description="What the shopper is told, in the merchant's words."
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmKitConfigCreateInput(BaseInputObjectType):
    collection = graphene.ID(required=True, description="The collection sold as a kit.")
    discount_kind = WsmKitDiscountKind(
        description="Money off the kit, or a share of what its members add up to."
    )
    discount_amount = PositiveDecimal(description="The saving off the members' prices.")
    freight_class = graphene.String(description="Freight class for the whole kit.")
    active = graphene.Boolean(description="Off takes the kit price away.")
    members = NonNullList(WsmKitMemberInput, description="The parts, and how many.")
    rules = NonNullList(WsmKitMemberRuleInput, description="How the kit goes together.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmKitConfigUpdateInput(BaseInputObjectType):
    discount_kind = WsmKitDiscountKind(
        description="Money off the kit, or a share of what its members add up to."
    )
    discount_amount = PositiveDecimal(description="The saving off the members' prices.")
    freight_class = graphene.String(description="Freight class for the whole kit.")
    active = graphene.Boolean(description="Off takes the kit price away.")
    members = NonNullList(
        WsmKitMemberInput,
        description="Omitted leaves members untouched. Provided replaces the list.",
    )
    rules = NonNullList(
        WsmKitMemberRuleInput,
        description="Omitted leaves rules untouched. Provided replaces the list.",
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class KitWriteMixin:
    """Everything a kit create and a kit update do identically, which is all of it.

    The only difference between the two is where the KitConfig row comes from,
    which is `get_instance`'s job. Members and rules are validated against each
    other here, before anything is written, so a refused payload leaves no kit
    row behind: the mutation runs inside one transaction and the first refusal
    takes the whole call with it.
    """

    @classmethod
    def perform_mutation(cls, root, info, /, **data):
        # One transaction over the kit, its parts and its rules. A payload that
        # is refused half way through its rules must not leave the members it
        # already replaced behind.
        with transaction.atomic():
            return super().perform_mutation(root, info, **data)

    @classmethod
    def clean_input(cls, info, instance, data, **kwargs):
        from ....graphql.core.context import get_database_connection_name

        database = get_database_connection_name(info.context)
        collection_id = data.pop("collection", None)
        cleaned_input = super().clean_input(info, instance, data, **kwargs)
        if collection_id is not None:
            cleaned_input["collection"] = _collection_or_error(database, collection_id)

        if cleaned_input.get("members") is not None:
            slots = cls._clean_members(database, instance, cleaned_input["members"])
            cleaned_input["member_slots"] = slots
            cleaned_input["replace_members"] = True
        else:
            cleaned_input["member_slots"] = [
                {"member": member, "key": None}
                for member in cls._stored_members(instance)
            ]
            cleaned_input["replace_members"] = False
        # Never handed to `construct_instance`: KitConfig has no such column.
        cleaned_input.pop("members", None)

        rules = cleaned_input.pop("rules", None)
        cleaned_input["rule_rows"] = (
            cls._clean_rules(instance, rules, cleaned_input["member_slots"])
            if rules is not None
            else None
        )
        return cleaned_input

    @staticmethod
    def _stored_members(instance):
        return list(instance.members.all()) if instance.pk else []

    @classmethod
    def _clean_members(cls, database, instance, rows):
        """The posted parts, checked against each other before one is written."""
        from ....product.models import ProductVariant

        stored = {member.pk: member for member in cls._stored_members(instance)}
        errors: dict = {}
        slots: list[dict] = []
        seen_keys: set = set()
        seen_variants: dict = {}

        for index, row in enumerate(rows):
            field = f"members.{index}"
            member = None
            if row.get("id"):
                pk = _pk_or_none(row["id"], "WsmKitMember")
                member = stored.get(pk) if pk is not None else None
                if member is None:
                    errors[f"{field}.id"] = _error(
                        "that part is not in this kit", "not_found"
                    )
                    continue
            key = row.get("key")
            if key is not None:
                if key in seen_keys:
                    errors[f"{field}.key"] = _error(
                        f"{key!r} names two parts in this payload",
                        "duplicated_input_item",
                    )
                    continue
                seen_keys.add(key)
            variant_pk = _pk_or_none(row["variant"], "ProductVariant")
            if variant_pk is None:
                errors[f"{field}.variant"] = _error(
                    "that is not a product variant", "invalid"
                )
                continue
            if variant_pk in seen_variants:
                errors[f"{field}.variant"] = _error(
                    "this kit already contains that part", "duplicate_kit_member"
                )
                continue
            seen_variants[variant_pk] = index
            quantity = row.get("quantity")
            quantity = 1 if quantity is None else quantity
            if quantity < 1:
                errors[f"{field}.quantity"] = _error(
                    "a kit contains at least one of each part",
                    "kit_member_quantity_below_one",
                )
                continue
            slots.append(
                {
                    "member": member,
                    "key": key,
                    "variant_pk": variant_pk,
                    "quantity": quantity,
                    "sort_order": row.get("sort_order") or 0,
                }
            )

        if errors:
            raise ValidationError(errors)

        variants = ProductVariant.objects.using(database).in_bulk(list(seen_variants))
        for slot in slots:
            variant = variants.get(slot["variant_pk"])
            if variant is None:
                errors[f"members.{seen_variants[slot['variant_pk']]}.variant"] = _error(
                    "that SKU does not exist", "not_found"
                )
            slot["variant"] = variant
        if errors:
            raise ValidationError(errors)
        return slots

    @classmethod
    def _clean_rules(cls, instance, rows, slots):
        """Every rule resolved to the SLOTS of this same kit, or refused.

        A rule naming a member outside this kit can never fire
        (`KitMemberRule.broken_by` intersects with the kit's own picks), so a
        `REQUIRES_ONE_OF` pointing outside makes the kit permanently unsellable
        and an `EXCLUDES` pointing outside never fires at all. Both are a
        merchant typing into a screen that lied to them.
        """
        stored_rules = (
            {rule.pk: rule for rule in instance.rules.all()} if instance.pk else {}
        )
        by_pk = {
            slot["member"].pk: index
            for index, slot in enumerate(slots)
            if slot["member"] is not None
        }
        by_key = {
            slot["key"]: index
            for index, slot in enumerate(slots)
            if slot["key"] is not None
        }
        errors: dict = {}
        parsed: list[dict] = []

        for index, row in enumerate(rows):
            field = f"rules.{index}"
            rule = None
            if row.get("id"):
                pk = _pk_or_none(row["id"], "WsmKitMemberRule")
                rule = stored_rules.get(pk) if pk is not None else None
                if rule is None:
                    errors[f"{field}.id"] = _error(
                        "that rule is not on this kit", "not_found"
                    )
                    continue

            subject_id, subject_key = row.get("subject_id"), row.get("subject_key")
            if subject_id and subject_key:
                errors[f"{field}.subjectId"] = _error(
                    "name the part by id or by key, never both", "invalid"
                )
                continue
            if not subject_id and not subject_key:
                errors[f"{field}.subjectId"] = _error(
                    "a rule is about one part; name it", "required"
                )
                continue
            if subject_id:
                subject = by_pk.get(_pk_or_none(subject_id, "WsmKitMember"))
            else:
                subject = by_key.get(subject_key)
            if subject is None:
                errors[f"{field}.subjectId"] = _error(
                    "that part is not in this kit", "rule_subject_not_in_kit"
                )
                continue

            targets: list[int] = []
            refused = False
            for target_id in row.get("target_ids") or []:
                target = by_pk.get(_pk_or_none(target_id, "WsmKitMember"))
                if target is None:
                    errors[f"{field}.targetIds"] = _error(
                        "that part is not in this kit", "rule_target_not_in_kit"
                    )
                    refused = True
                    break
                targets.append(target)
            if refused:
                continue
            for target_key in row.get("target_keys") or []:
                target = by_key.get(target_key)
                if target is None:
                    errors[f"{field}.targetKeys"] = _error(
                        f"{target_key!r} names no part in this payload",
                        "rule_target_not_in_kit",
                    )
                    refused = True
                    break
                targets.append(target)
            if refused:
                continue

            message = (row.get("message") or "").strip()
            if not message:
                errors[f"{field}.message"] = _error(
                    "say what the shopper is told", "required"
                )
                continue

            parsed.append(
                {
                    "rule": rule,
                    "subject": subject,
                    "kind": row["kind"],
                    "targets": targets,
                    "message": message,
                }
            )

        if errors:
            raise ValidationError(errors)
        return parsed

    @classmethod
    def _save_m2m(cls, info, instance, cleaned_data):
        super()._save_m2m(info, instance, cleaned_data)
        if cleaned_data.get("replace_members"):
            cls._write_members(instance, cleaned_data["member_slots"])
        if cleaned_data.get("rule_rows") is not None:
            cls._write_rules(
                instance, cleaned_data["rule_rows"], cleaned_data["member_slots"]
            )

    @staticmethod
    def _write_members(instance, slots):
        """Replace the set: the dropped rows go FIRST, then the kept ones move.

        Dropped first because `wsm_containers_one_row_per_kit_variant` is a
        database constraint, so a row taking over a variant another row is
        giving up has to find it gone.
        ponytail: the ceiling is two KEPT rows swapping variants with each
        other, which still collides. The upgrade, the day a merchant screen
        offers a swap, is a deferrable constraint; a datagrid that deletes and
        adds, which is what the Dashboard's does, never reaches it.
        """
        kept = [slot["member"].pk for slot in slots if slot["member"] is not None]
        instance.members.exclude(pk__in=kept).delete()
        for slot in slots:
            member = slot["member"] or models.KitMember(kit=instance)
            member.variant = slot["variant"]
            member.quantity = slot["quantity"]
            member.sort_order = slot["sort_order"]
            member.save()
            slot["member"] = member

    @staticmethod
    def _write_rules(instance, rule_rows, slots):
        kept = [row["rule"].pk for row in rule_rows if row["rule"] is not None]
        instance.rules.exclude(pk__in=kept).delete()
        for row in rule_rows:
            rule = row["rule"] or models.KitMemberRule(kit=instance)
            rule.subject = slots[row["subject"]]["member"]
            rule.kind = row["kind"]
            rule.message = row["message"]
            rule.save()
            rule.targets.set([slots[index]["member"] for index in row["targets"]])


class WsmKitConfigCreate(KitWriteMixin, DeprecatedModelMutation):
    class Arguments:
        input = WsmKitConfigCreateInput(
            required=True, description="Fields required to create a kit."
        )

    class Meta:
        description = "Create a kit, its parts and its rules in one call."
        model = models.KitConfig
        object_type = WsmKitConfig
        return_field_name = "kit"
        permissions = CONTAINER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class WsmKitConfigUpdate(KitWriteMixin, DeprecatedModelMutation):
    class Arguments:
        id = graphene.ID(required=True, description="ID of the kit to update.")
        input = WsmKitConfigUpdateInput(
            required=True, description="Fields required to update a kit."
        )

    class Meta:
        description = "Update a kit. A provided member or rule list replaces the set."
        model = models.KitConfig
        object_type = WsmKitConfig
        return_field_name = "kit"
        permissions = CONTAINER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM


class WsmKitConfigDelete(ModelDeleteMutation):
    class Arguments:
        id = graphene.ID(required=True, description="ID of the kit to delete.")

    class Meta:
        description = (
            "Delete a kit. Its parts and rules go with it; the collection and "
            "its products are untouched, because a kit is a row ABOUT a "
            "collection and never the collection itself."
        )
        model = models.KitConfig
        object_type = WsmKitConfig
        return_field_name = "kit"
        permissions = CONTAINER_PERMISSIONS
        error_type_class = WsmError
        doc_category = DOC_CATEGORY_WSM
