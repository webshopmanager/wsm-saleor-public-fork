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
from ....graphql.core.types import BaseInputObjectType, NonNullList
from ....permission.enums import ProductPermissions
from ...containers import models
from ..errors import WsmError
from ..scalars import WsmDecimal
from ..types import DOC_CATEGORY_WSM
from ..utils import error as _error
from ..utils import pk_or_none as _pk_or_none
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


class WsmContainerSlotInput(BaseInputObjectType):
    id = graphene.ID(description="Omit to create. Supply to edit the row in place.")
    key = graphene.String(
        description=(
            "A client-chosen handle, unique within this one mutation, so a part "
            "in the same payload can name a slot that has no ID yet. Never stored."
        )
    )
    label = graphene.String(
        required=True, description="What this role is called: Exhaust, Tuner, Gauge."
    )
    quantity = graphene.Int(description="How many of whatever fills this slot.")
    required = graphene.Boolean(
        description="A required slot with nothing that fits refuses the container."
    )
    sort_order = graphene.Int(description="Lowest first.")
    axes = NonNullList(
        graphene.String,
        description="The questions this slot asks AFTER the vehicle, in order.",
    )
    partitioning_axis = graphene.String(
        description=(
            "The axis that decides which of the fitting candidates the shopper "
            "ends up on. Blank takes every candidate that fits."
        )
    )
    miss_message = graphene.String(
        description="What a shopper is told when nothing here fits their vehicle."
    )
    source_collection = graphene.ID(
        description=(
            "Fill this slot from another container's collection instead of "
            "listing candidates, so a kit can hold a series by reference."
        )
    )

    class Meta:
        doc_category = DOC_CATEGORY_WSM


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
    slot_id = graphene.ID(
        description="An existing slot of this container. Never both with slotKey."
    )
    slot_key = graphene.String(
        description="A slot created in this same payload. Never both with slotId."
    )

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
    discount_amount = WsmDecimal(description="The saving off the members' prices.")
    freight_class = graphene.String(description="Freight class for the whole kit.")
    active = graphene.Boolean(description="Off takes the kit price away.")
    brand = graphene.String(description="The one brand this container covers.")
    published = graphene.Boolean(description="Show this container as its own page.")
    miss_message = graphene.String(
        description="What a shopper is told when their vehicle leaves a slot empty."
    )
    slots = NonNullList(
        WsmContainerSlotInput,
        description=(
            "The roles this container is made of. Provided REPLACES the list, "
            "and a slot dropped from it takes its own candidates with it, so a "
            "payload that drops a slot still holding parts is refused unless it "
            "replaces the parts too."
        ),
    )
    members = NonNullList(WsmKitMemberInput, description="The parts, and how many.")
    rules = NonNullList(WsmKitMemberRuleInput, description="How the kit goes together.")

    class Meta:
        doc_category = DOC_CATEGORY_WSM


class WsmKitConfigUpdateInput(BaseInputObjectType):
    discount_kind = WsmKitDiscountKind(
        description="Money off the kit, or a share of what its members add up to."
    )
    discount_amount = WsmDecimal(description="The saving off the members' prices.")
    freight_class = graphene.String(description="Freight class for the whole kit.")
    active = graphene.Boolean(description="Off takes the kit price away.")
    brand = graphene.String(description="The one brand this container covers.")
    published = graphene.Boolean(description="Show this container as its own page.")
    miss_message = graphene.String(
        description="What a shopper is told when their vehicle leaves a slot empty."
    )
    slots = NonNullList(
        WsmContainerSlotInput,
        description=(
            "Omitted leaves slots untouched. Provided replaces the list, and a "
            "slot dropped from it takes its own candidates with it."
        ),
    )
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

        slots = cleaned_input.pop("slots", None)
        cleaned_input["slot_rows"] = (
            cls._clean_slots(database, instance, slots) if slots is not None else None
        )
        cleaned_input["replace_slots"] = slots is not None

        if cleaned_input.get("members") is not None:
            cleaned_input["member_rows"] = cls._clean_members(
                database, instance, cleaned_input["members"], cleaned_input["slot_rows"]
            )
            cleaned_input["replace_members"] = True
        else:
            cleaned_input["member_rows"] = [
                {"member": member, "key": None, "slot": None}
                for member in cls._stored_members(instance)
            ]
            cleaned_input["replace_members"] = False
        # Never handed to `construct_instance`: KitConfig has no such column.
        cleaned_input.pop("members", None)

        if cleaned_input["replace_slots"] and not cleaned_input["replace_members"]:
            cls._refuse_orphaning_candidates(instance, cleaned_input["slot_rows"])

        rules = cleaned_input.pop("rules", None)
        cleaned_input["rule_rows"] = (
            cls._clean_rules(instance, rules, cleaned_input["member_rows"])
            if rules is not None
            else None
        )
        return cleaned_input

    @staticmethod
    def _stored_members(instance):
        return list(instance.members.all()) if instance.pk else []

    @staticmethod
    def _stored_slots(instance):
        return list(instance.slots.all()) if instance.pk else []

    @classmethod
    def _clean_slots(cls, database, instance, rows):
        """The posted roles, checked against each other before one is written."""
        from ....product.models import Collection

        stored = {slot.pk: slot for slot in cls._stored_slots(instance)}
        errors: dict = {}
        slot_rows: list[dict] = []
        seen_keys: set = set()
        seen_labels: dict = {}
        wanted_sources: dict = {}

        for index, row in enumerate(rows):
            field = f"slots.{index}"
            slot = None
            if row.get("id"):
                pk = _pk_or_none(row["id"], "WsmContainerSlot")
                slot = stored.get(pk) if pk is not None else None
                if slot is None:
                    errors[f"{field}.id"] = _error(
                        "that slot is not on this container", "not_found"
                    )
                    continue
            key = row.get("key")
            if key is not None:
                if key in seen_keys:
                    errors[f"{field}.key"] = _error(
                        f"{key!r} names two slots in this payload",
                        "duplicated_input_item",
                    )
                    continue
                seen_keys.add(key)

            label = (row.get("label") or "").strip()
            if not label:
                errors[f"{field}.label"] = _error("name this role", "required")
                continue
            # The database says so too (wsm_containers_one_slot_per_label); this
            # is the same rule where a merchant can read it, on the field.
            if label.casefold() in seen_labels:
                errors[f"{field}.label"] = _error(
                    f"this container already has a slot called {label!r}",
                    "duplicate_slot_label",
                )
                continue
            seen_labels[label.casefold()] = index

            quantity = row.get("quantity")
            quantity = 1 if quantity is None else quantity
            if quantity < 1:
                errors[f"{field}.quantity"] = _error(
                    "a slot holds at least one of whatever fills it",
                    "kit_member_quantity_below_one",
                )
                continue

            axes = list(row.get("axes") or [])
            partitioning_axis = row.get("partitioning_axis") or ""
            if partitioning_axis and axes and partitioning_axis not in axes:
                errors[f"{field}.partitioningAxis"] = _error(
                    f"{partitioning_axis!r} is not one of the axes {axes!r}",
                    "axis_not_in_axes",
                )
                continue

            source = row.get("source_collection")
            source_pk = None
            if source:
                source_pk = _pk_or_none(source, "Collection")
                if source_pk is None:
                    errors[f"{field}.sourceCollection"] = _error(
                        "that is not a collection", "invalid"
                    )
                    continue
                wanted_sources.setdefault(source_pk, index)

            slot_rows.append(
                {
                    "slot": slot,
                    "key": key,
                    "label": label,
                    "quantity": quantity,
                    "required": True
                    if row.get("required") is None
                    else row["required"],
                    "sort_order": row.get("sort_order") or 0,
                    "axes": axes,
                    "partitioning_axis": partitioning_axis,
                    "miss_message": row.get("miss_message") or "",
                    "source_collection_pk": source_pk,
                }
            )

        if errors:
            raise ValidationError(errors)

        # One read for every source collection the payload names, never one per
        # slot: a bundle of twelve series would otherwise be twelve queries.
        sources = Collection.objects.using(database).in_bulk(list(wanted_sources))
        for slot_row in slot_rows:
            source_pk = slot_row["source_collection_pk"]
            if source_pk is None:
                slot_row["source_collection"] = None
                continue
            collection = sources.get(source_pk)
            if collection is None:
                errors[f"slots.{wanted_sources[source_pk]}.sourceCollection"] = _error(
                    "that collection does not exist", "not_found"
                )
            slot_row["source_collection"] = collection
        if errors:
            raise ValidationError(errors)
        return slot_rows

    @staticmethod
    def _slot_for_member(row, slot_rows):
        """Which posted slot this part fills, as an index, or (None, error).

        Named by id or by key, never both, and always a slot IN THIS PAYLOAD: a
        part pointing at a role the container is about to stop having is a row
        the cascade would delete the moment it was written.
        """
        slot_id, slot_key = row.get("slot_id"), row.get("slot_key")
        if slot_id and slot_key:
            return None, _error("name the slot by id or by key, never both", "invalid")
        if not slot_id and not slot_key:
            return None, None
        if slot_rows is None:
            return None, _error(
                "name this container's slots in the same call", "slot_not_in_container"
            )
        if slot_id:
            pk = _pk_or_none(slot_id, "WsmContainerSlot")
            for index, slot_row in enumerate(slot_rows):
                if slot_row["slot"] is not None and slot_row["slot"].pk == pk:
                    return index, None
        else:
            for index, slot_row in enumerate(slot_rows):
                if slot_row["key"] == slot_key:
                    return index, None
        return None, _error(
            "that slot is not on this container", "slot_not_in_container"
        )

    @classmethod
    def _refuse_orphaning_candidates(cls, instance, slot_rows):
        """Dropping a role deletes the parts that fill it. Never by surprise.

        `KitMember.slot` cascades on purpose: a candidate for a role that no
        longer exists is not a part of anything. That is right when the merchant
        is rewriting the whole container, and it is silent data loss when they
        only touched the slot list, so this refuses the second case out loud and
        names the slot.
        """
        if not instance.pk:
            return
        kept = {row["slot"].pk for row in slot_rows if row["slot"] is not None}
        doomed = [
            slot
            for slot in cls._stored_slots(instance)
            if slot.pk not in kept and slot.candidates.exists()
        ]
        if doomed:
            raise ValidationError(
                {
                    "slots": _error(
                        "these slots still hold parts, so removing them would "
                        "delete those parts: "
                        f"{sorted(slot.label for slot in doomed)}. Send the "
                        "parts you want to keep in the same call.",
                        "slot_still_has_candidates",
                    )
                }
            )

    @classmethod
    def _clean_members(cls, database, instance, rows, slot_rows):
        """The posted parts, checked against each other before one is written."""
        from ....product.models import ProductVariant

        stored = {member.pk: member for member in cls._stored_members(instance)}
        errors: dict = {}
        member_rows: list[dict] = []
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
            slot_index, slot_error = cls._slot_for_member(row, slot_rows)
            if slot_error is not None:
                errors[f"{field}.slotId"] = slot_error
                continue
            member_rows.append(
                {
                    "member": member,
                    "key": key,
                    "variant_pk": variant_pk,
                    "quantity": quantity,
                    "sort_order": row.get("sort_order") or 0,
                    "slot_index": slot_index,
                }
            )

        if errors:
            raise ValidationError(errors)

        variants = ProductVariant.objects.using(database).in_bulk(list(seen_variants))
        for member_row in member_rows:
            variant = variants.get(member_row["variant_pk"])
            if variant is None:
                index = seen_variants[member_row["variant_pk"]]
                errors[f"members.{index}.variant"] = _error(
                    "that SKU does not exist", "not_found"
                )
            member_row["variant"] = variant
        if errors:
            raise ValidationError(errors)
        return member_rows

    @classmethod
    def _clean_rules(cls, instance, rows, member_rows):
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
            row["member"].pk: index
            for index, row in enumerate(member_rows)
            if row["member"] is not None
        }
        by_key = {
            row["key"]: index
            for index, row in enumerate(member_rows)
            if row["key"] is not None
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
        # Slots first: a member row names the slot it fills, so the slot has to
        # have a primary key before the member is saved against it.
        if cleaned_data.get("replace_slots"):
            cls._write_slots(instance, cleaned_data["slot_rows"])
        if cleaned_data.get("replace_members"):
            cls._write_members(
                instance, cleaned_data["member_rows"], cleaned_data["slot_rows"]
            )
        if cleaned_data.get("rule_rows") is not None:
            cls._write_rules(
                instance, cleaned_data["rule_rows"], cleaned_data["member_rows"]
            )

    @staticmethod
    def _write_slots(instance, slot_rows):
        """Replace the set. Dropped rows go FIRST, for the unique label constraint.

        Same shape and same reason as `_write_members`: `wsm_containers_one_slot_per_label`
        is a database constraint, so a row taking over a label another row is
        giving up has to find it gone.
        """
        kept = [row["slot"].pk for row in slot_rows if row["slot"] is not None]
        instance.slots.exclude(pk__in=kept).delete()
        for row in slot_rows:
            slot = row["slot"] or models.ContainerSlot(kit=instance)
            slot.label = row["label"]
            slot.quantity = row["quantity"]
            slot.required = row["required"]
            slot.sort_order = row["sort_order"]
            slot.axes = row["axes"]
            slot.partitioning_axis = row["partitioning_axis"]
            slot.miss_message = row["miss_message"]
            slot.source_collection = row["source_collection"]
            slot.save()
            row["slot"] = slot

    @staticmethod
    def _write_members(instance, member_rows, slot_rows=None):
        """Replace the set: the dropped rows go FIRST, then the kept ones move.

        Dropped first because `wsm_containers_one_row_per_kit_variant` is a
        database constraint, so a row taking over a variant another row is
        giving up has to find it gone.
        ponytail: the ceiling is two KEPT rows swapping variants with each
        other, which still collides. The upgrade, the day a merchant screen
        offers a swap, is a deferrable constraint; a datagrid that deletes and
        adds, which is what the Dashboard's does, never reaches it.
        """
        kept = [row["member"].pk for row in member_rows if row["member"] is not None]
        instance.members.exclude(pk__in=kept).delete()
        for row in member_rows:
            member = row["member"] or models.KitMember(kit=instance)
            member.variant = row["variant"]
            member.quantity = row["quantity"]
            member.sort_order = row["sort_order"]
            if "slot_index" in row:
                index = row["slot_index"]
                member.slot = (
                    slot_rows[index]["slot"]
                    if index is not None and slot_rows is not None
                    else None
                )
            member.save()
            row["member"] = member

    @staticmethod
    def _write_rules(instance, rule_rows, member_rows):
        kept = [row["rule"].pk for row in rule_rows if row["rule"] is not None]
        instance.rules.exclude(pk__in=kept).delete()
        for row in rule_rows:
            rule = row["rule"] or models.KitMemberRule(kit=instance)
            rule.subject = member_rows[row["subject"]]["member"]
            rule.kind = row["kind"]
            rule.message = row["message"]
            rule.save()
            rule.targets.set([member_rows[index]["member"] for index in row["targets"]])


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
