# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The kit editor over GraphQL: one payload carries the kit, its parts and its rules.

Members and rules have no top-level mutations, for the reason the admin had no
standalone screens for them: a rule is about the parts beside it, so the set is
what has to be checked at once. Provided means REPLACE; omitted means leave
alone.

`RULE_TARGET_NOT_IN_KIT` is the one rule here nothing enforced before. The admin
scoped the target picker to this kit's own members
(`containers/admin.py:243`) and the model never looked, because `targets` is an
m2m that is written after `clean()` has run. This mutation is the first place
it is checked at all, so its test was written and watched fail before the check
existed.
"""

from decimal import Decimal

import graphene
import pytest

from .....graphql.tests.utils import assert_no_permission, get_graphql_content
from ....containers import pricing
from ....containers.models import (
    EXCLUDES,
    REQUIRES_ONE_OF,
    KitConfig,
    KitMember,
    KitMemberRule,
)

pytestmark = pytest.mark.django_db


CREATE = """
    mutation Create($input: WsmKitConfigCreateInput!) {
      wsmKitConfigCreate(input: $input) {
        kit {
          id
          discountKind
          discountAmount
          freightClass
          active
          currencyCode
          collection { id name }
          members {
            id
            quantity
            sortOrder
            variant { id sku product { name } }
          }
          rules {
            id
            kind
            message
            subject { id variant { sku } }
            targets { id variant { sku } }
          }
        }
        errors { field code message }
      }
    }
"""

UPDATE = """
    mutation Update($id: ID!, $input: WsmKitConfigUpdateInput!) {
      wsmKitConfigUpdate(id: $id, input: $input) {
        kit {
          id
          discountKind
          discountAmount
          freightClass
          active
          members { id quantity sortOrder variant { id } }
          rules { id kind message subject { id } targets { id } }
        }
        errors { field code message }
      }
    }
"""

DELETE = """
    mutation Delete($id: ID!) {
      wsmKitConfigDelete(id: $id) {
        kit { id }
        errors { field code message }
      }
    }
"""

DETAIL = """
    query Detail($id: ID, $collection: ID) {
      wsmKitConfig(id: $id, collection: $collection) {
        id
        discountKind
        discountAmount
        freightClass
        members { id quantity variant { id } }
        rules { id kind targets { id } }
      }
    }
"""

LIST = """
    query List($filter: WsmKitConfigFilterInput) {
      wsmKitConfigs(filter: $filter, first: 20) {
        totalCount
        edges { node { id active collection { name } } }
      }
    }
"""


def gid(type_name, pk):
    return graphene.Node.to_global_id(type_name, pk)


def variant_ids(product_list):
    return [gid("ProductVariant", p.variants.first().pk) for p in product_list]


def create_input(collection, product_list, **overrides):
    payload = {
        "collection": gid("Collection", collection.pk),
        "discountKind": "PERCENT",
        "discountAmount": "10.00",
        "freightClass": "70",
        "active": True,
        "members": [
            {
                "key": "cover",
                "variant": variant_ids(product_list)[0],
                "quantity": 1,
                "sortOrder": 0,
            },
            {
                "key": "tensioner",
                "variant": variant_ids(product_list)[1],
                "quantity": 2,
                "sortOrder": 1,
            },
        ],
        "rules": [
            {
                "subjectKey": "cover",
                "kind": "REQUIRES_ONE_OF",
                "targetKeys": ["tensioner"],
                "message": "Requires the Manual or HD Tensioner.",
            }
        ],
    }
    payload.update(overrides)
    return payload


def make_kit(collection, product_list, **overrides):
    """A stored kit with two members, built without the mutation under test."""
    fields = {"discount_kind": pricing.FIXED, "discount_amount": Decimal("5.00")}
    fields.update(overrides)
    kit = KitConfig.objects.create(collection=collection, **fields)
    for order, product in enumerate(product_list[:2]):
        KitMember.objects.create(
            kit=kit, variant=product.variants.first(), quantity=1, sort_order=order
        )
    return kit


# --- permissions -------------------------------------------------------------


def test_an_anonymous_caller_cannot_read_a_kit(api_client, collection):
    response = api_client.post_graphql(
        DETAIL, {"collection": gid("Collection", collection.pk)}
    )

    assert_no_permission(response)


def test_the_create_is_refused_without_the_permission(
    staff_api_client, collection, product_list
):
    response = staff_api_client.post_graphql(
        CREATE, {"input": create_input(collection, product_list)}
    )

    assert_no_permission(response)
    assert not KitConfig.objects.exists()


def test_the_update_is_refused_without_the_permission(
    staff_api_client, collection, product_list
):
    kit = make_kit(collection, product_list)

    response = staff_api_client.post_graphql(
        UPDATE, {"id": gid("WsmKitConfig", kit.pk), "input": {"active": False}}
    )

    assert_no_permission(response)
    kit.refresh_from_db()
    assert kit.active is True


def test_the_delete_is_refused_without_the_permission(
    staff_api_client, collection, product_list
):
    kit = make_kit(collection, product_list)

    response = staff_api_client.post_graphql(
        DELETE, {"id": gid("WsmKitConfig", kit.pk)}
    )

    assert_no_permission(response)
    assert KitConfig.objects.filter(pk=kit.pk).exists()


# --- create: the whole kit in one payload ------------------------------------


def test_the_create_lands_the_kit_its_members_and_a_rule_in_one_call(
    staff_api_client, permission_manage_products, collection, product_list
):
    """The create screen's whole job: rules point at members by `key`.

    Both are new in the same call, so there is no id for a rule to name; the
    key is a handle for this one mutation and is never stored.
    """
    response = staff_api_client.post_graphql(
        CREATE,
        {"input": create_input(collection, product_list)},
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigCreate"]
    assert payload["errors"] == []
    kit = payload["kit"]
    assert kit["discountKind"] == "PERCENT"
    assert Decimal(kit["discountAmount"]) == Decimal("10.00")
    assert kit["freightClass"] == "70"
    assert kit["active"] is True
    assert kit["collection"]["name"] == collection.name
    assert [member["quantity"] for member in kit["members"]] == [1, 2]
    assert [member["sortOrder"] for member in kit["members"]] == [0, 1]
    assert [member["variant"]["id"] for member in kit["members"]] == variant_ids(
        product_list
    )[:2]

    assert len(kit["rules"]) == 1
    rule = kit["rules"][0]
    assert rule["kind"] == "REQUIRES_ONE_OF"
    assert rule["message"] == "Requires the Manual or HD Tensioner."
    assert rule["subject"]["id"] == kit["members"][0]["id"]
    assert [target["id"] for target in rule["targets"]] == [kit["members"][1]["id"]]

    stored = KitConfig.objects.get()
    assert stored.members.count() == 2
    assert stored.rules.get().targets.count() == 1


def test_the_created_kit_reads_back_the_same_through_the_detail_query(
    staff_api_client, permission_manage_products, collection, product_list
):
    """Round trip: what the create returned is what the detail screen loads."""
    staff_api_client.user.user_permissions.add(permission_manage_products)
    created = get_graphql_content(
        staff_api_client.post_graphql(
            CREATE, {"input": create_input(collection, product_list)}
        )
    )["data"]["wsmKitConfigCreate"]["kit"]

    read = get_graphql_content(
        staff_api_client.post_graphql(DETAIL, {"id": created["id"]})
    )["data"]["wsmKitConfig"]

    assert read["id"] == created["id"]
    assert read["freightClass"] == "70"
    assert [m["variant"]["id"] for m in read["members"]] == [
        m["variant"]["id"] for m in created["members"]
    ]
    assert [t["id"] for t in read["rules"][0]["targets"]] == [
        t["id"] for t in created["rules"][0]["targets"]
    ]


def test_a_second_kit_on_the_same_collection_is_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    make_kit(collection, product_list)

    response = staff_api_client.post_graphql(
        CREATE,
        {"input": create_input(collection, product_list)},
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigCreate"]
    assert [error["code"] for error in payload["errors"]] == ["UNIQUE"]
    assert KitConfig.objects.count() == 1


# --- update: provided replaces, omitted leaves alone -------------------------


def test_the_update_replaces_the_whole_member_set(
    staff_api_client, permission_manage_products, collection, product_list
):
    """Rows absent from the posted list are gone, which is the contract."""
    kit = make_kit(collection, product_list)
    first, second = kit.members.order_by("sort_order")

    response = staff_api_client.post_graphql(
        UPDATE,
        {
            "id": gid("WsmKitConfig", kit.pk),
            "input": {
                "members": [
                    {
                        "id": gid("WsmKitMember", first.pk),
                        "variant": gid("ProductVariant", first.variant_id),
                        "quantity": 4,
                        "sortOrder": 0,
                    },
                    {
                        "variant": variant_ids(product_list)[2],
                        "quantity": 1,
                        "sortOrder": 1,
                    },
                ]
            },
        },
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]
    assert payload["errors"] == []
    assert [member["quantity"] for member in payload["kit"]["members"]] == [4, 1]
    assert kit.members.count() == 2
    assert not KitMember.objects.filter(pk=second.pk).exists(), "the dropped row stayed"
    assert KitMember.objects.filter(pk=first.pk).get().quantity == 4


def test_omitting_members_leaves_them_untouched(
    staff_api_client, permission_manage_products, collection, product_list
):
    kit = make_kit(collection, product_list)

    response = staff_api_client.post_graphql(
        UPDATE,
        {"id": gid("WsmKitConfig", kit.pk), "input": {"discountAmount": "12.50"}},
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]
    assert payload["errors"] == []
    assert Decimal(payload["kit"]["discountAmount"]) == Decimal("12.50")
    assert len(payload["kit"]["members"]) == 2
    assert kit.members.count() == 2


def test_the_update_replaces_the_whole_rule_set(
    staff_api_client, permission_manage_products, collection, product_list
):
    kit = make_kit(collection, product_list)
    first, second = kit.members.order_by("sort_order")
    doomed = KitMemberRule.objects.create(
        kit=kit, subject=first, kind=REQUIRES_ONE_OF, message="The old sentence."
    )
    doomed.targets.set([second])

    response = staff_api_client.post_graphql(
        UPDATE,
        {
            "id": gid("WsmKitConfig", kit.pk),
            "input": {
                "rules": [
                    {
                        "subjectId": gid("WsmKitMember", second.pk),
                        "kind": "EXCLUDES",
                        "targetIds": [gid("WsmKitMember", first.pk)],
                        "message": "Cannot be fitted with the OEM cover.",
                    }
                ]
            },
        },
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]
    assert payload["errors"] == []
    assert len(payload["kit"]["rules"]) == 1
    assert payload["kit"]["rules"][0]["kind"] == "EXCLUDES"
    assert not KitMemberRule.objects.filter(pk=doomed.pk).exists()
    assert KitMemberRule.objects.get().kind == EXCLUDES


def test_an_empty_member_list_empties_the_kit(
    staff_api_client, permission_manage_products, collection, product_list
):
    """Provided-and-empty is a provided list, not an omission."""
    kit = make_kit(collection, product_list)

    response = staff_api_client.post_graphql(
        UPDATE,
        {"id": gid("WsmKitConfig", kit.pk), "input": {"members": []}},
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]
    assert payload["errors"] == []
    assert payload["kit"]["members"] == []
    assert kit.members.count() == 0


# --- the refusals, each with its own code ------------------------------------


def test_a_member_quantity_below_one_is_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    payload_input = create_input(collection, product_list)
    payload_input["members"][1]["quantity"] = 0

    response = staff_api_client.post_graphql(
        CREATE, {"input": payload_input}, permissions=[permission_manage_products]
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigCreate"]
    assert [error["code"] for error in payload["errors"]] == [
        "KIT_MEMBER_QUANTITY_BELOW_ONE"
    ]
    assert payload["errors"][0]["field"] == "members.1.quantity"
    assert not KitConfig.objects.exists(), "the kit row survived a refused payload"


def test_two_rows_for_the_same_variant_are_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    payload_input = create_input(collection, product_list)
    payload_input["members"][1]["variant"] = payload_input["members"][0]["variant"]

    response = staff_api_client.post_graphql(
        CREATE, {"input": payload_input}, permissions=[permission_manage_products]
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigCreate"]
    assert [error["code"] for error in payload["errors"]] == ["DUPLICATE_KIT_MEMBER"]
    assert payload["errors"][0]["field"] == "members.1.variant"
    assert not KitConfig.objects.exists()


def test_two_members_sharing_one_key_are_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    payload_input = create_input(collection, product_list)
    payload_input["members"][1]["key"] = payload_input["members"][0]["key"]

    response = staff_api_client.post_graphql(
        CREATE, {"input": payload_input}, permissions=[permission_manage_products]
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigCreate"]
    assert [error["code"] for error in payload["errors"]] == ["DUPLICATED_INPUT_ITEM"]
    assert payload["errors"][0]["field"] == "members.1.key"


def test_a_rule_whose_subject_belongs_to_another_kit_is_refused(
    staff_api_client,
    permission_manage_products,
    collection,
    published_collection,
    product_list,
):
    kit = make_kit(collection, product_list)
    stranger = make_kit(published_collection, product_list[2:])
    outsider = stranger.members.first()

    response = staff_api_client.post_graphql(
        UPDATE,
        {
            "id": gid("WsmKitConfig", kit.pk),
            "input": {
                "rules": [
                    {
                        "subjectId": gid("WsmKitMember", outsider.pk),
                        "kind": "EXCLUDES",
                        "targetIds": [gid("WsmKitMember", kit.members.first().pk)],
                        "message": "Nobody can satisfy this.",
                    }
                ]
            },
        },
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]
    assert [error["code"] for error in payload["errors"]] == ["RULE_SUBJECT_NOT_IN_KIT"]
    assert payload["errors"][0]["field"] == "rules.0.subjectId"
    assert not KitMemberRule.objects.exists()


def test_a_rule_target_that_belongs_to_another_kit_is_refused(
    staff_api_client,
    permission_manage_products,
    collection,
    published_collection,
    product_list,
):
    """The check nothing had: the admin scoped the picker, the model never looked.

    A target outside this kit can never be present when the rule is evaluated
    (`KitMemberRule.broken_by` intersects with the kit's own picks), so a
    `REQUIRES_ONE_OF` pointing outside makes the kit permanently unsellable and
    an `EXCLUDES` pointing outside is a rule that never fires. Both are a
    merchant typing into a screen that lied to them.
    """
    kit = make_kit(collection, product_list)
    stranger = make_kit(published_collection, product_list[2:])
    outsider = stranger.members.first()
    subject = kit.members.first()

    response = staff_api_client.post_graphql(
        UPDATE,
        {
            "id": gid("WsmKitConfig", kit.pk),
            "input": {
                "rules": [
                    {
                        "subjectId": gid("WsmKitMember", subject.pk),
                        "kind": "REQUIRES_ONE_OF",
                        "targetIds": [gid("WsmKitMember", outsider.pk)],
                        "message": "Requires a part of some other kit.",
                    }
                ]
            },
        },
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]
    assert [error["code"] for error in payload["errors"]] == ["RULE_TARGET_NOT_IN_KIT"]
    assert payload["errors"][0]["field"] == "rules.0.targetIds"
    assert not KitMemberRule.objects.exists()


def test_a_rule_naming_a_key_no_member_carries_is_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    payload_input = create_input(collection, product_list)
    payload_input["rules"][0]["targetKeys"] = ["a_key_nobody_posted"]

    response = staff_api_client.post_graphql(
        CREATE, {"input": payload_input}, permissions=[permission_manage_products]
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigCreate"]
    assert [error["code"] for error in payload["errors"]] == ["RULE_TARGET_NOT_IN_KIT"]
    assert not KitConfig.objects.exists()


def test_a_rule_that_names_no_subject_at_all_is_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    payload_input = create_input(collection, product_list)
    payload_input["rules"][0].pop("subjectKey")

    response = staff_api_client.post_graphql(
        CREATE, {"input": payload_input}, permissions=[permission_manage_products]
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigCreate"]
    assert [error["code"] for error in payload["errors"]] == ["REQUIRED"]
    assert payload["errors"][0]["field"] == "rules.0.subjectId"


def test_a_rule_naming_a_subject_twice_over_is_refused(
    staff_api_client, permission_manage_products, collection, product_list
):
    payload_input = create_input(collection, product_list)
    payload_input["rules"][0]["subjectId"] = gid("WsmKitMember", 1)

    response = staff_api_client.post_graphql(
        CREATE, {"input": payload_input}, permissions=[permission_manage_products]
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigCreate"]
    assert [error["code"] for error in payload["errors"]] == ["INVALID"]


def test_a_member_row_naming_another_kits_row_is_refused(
    staff_api_client,
    permission_manage_products,
    collection,
    published_collection,
    product_list,
):
    kit = make_kit(collection, product_list)
    stranger = make_kit(published_collection, product_list[2:])
    outsider = stranger.members.first()

    response = staff_api_client.post_graphql(
        UPDATE,
        {
            "id": gid("WsmKitConfig", kit.pk),
            "input": {
                "members": [
                    {
                        "id": gid("WsmKitMember", outsider.pk),
                        "variant": gid("ProductVariant", outsider.variant_id),
                        "quantity": 1,
                    }
                ]
            },
        },
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]
    assert [error["code"] for error in payload["errors"]] == ["NOT_FOUND"]
    assert payload["errors"][0]["field"] == "members.0.id"
    assert stranger.members.filter(pk=outsider.pk).exists(), "another kit lost a part"


# --- delete ------------------------------------------------------------------


def test_the_delete_removes_the_kit_its_members_and_its_rules(
    staff_api_client, permission_manage_products, collection, product_list
):
    kit = make_kit(collection, product_list)
    first, second = kit.members.order_by("sort_order")
    rule = KitMemberRule.objects.create(
        kit=kit, subject=first, kind=EXCLUDES, message="No."
    )
    rule.targets.set([second])

    response = staff_api_client.post_graphql(
        DELETE,
        {"id": gid("WsmKitConfig", kit.pk)},
        permissions=[permission_manage_products],
    )

    payload = get_graphql_content(response)["data"]["wsmKitConfigDelete"]
    assert payload["errors"] == []
    assert payload["kit"]["id"] == gid("WsmKitConfig", kit.pk)
    assert not KitConfig.objects.exists()
    assert not KitMember.objects.exists()
    assert not KitMemberRule.objects.exists()


def test_deleting_a_kit_leaves_the_collection_and_its_products_alone(
    staff_api_client, permission_manage_products, collection, product_list
):
    """A kit is a row ABOUT a collection, never the collection itself."""
    kit = make_kit(collection, product_list)

    staff_api_client.post_graphql(
        DELETE,
        {"id": gid("WsmKitConfig", kit.pk)},
        permissions=[permission_manage_products],
    )

    collection.refresh_from_db()
    assert collection.pk is not None
    assert product_list[0].variants.exists()


# --- the read surface --------------------------------------------------------


def test_the_detail_answers_by_collection_and_by_id(
    staff_api_client, permission_manage_products, collection, product_list
):
    kit = make_kit(collection, product_list)
    staff_api_client.user.user_permissions.add(permission_manage_products)

    by_collection = get_graphql_content(
        staff_api_client.post_graphql(
            DETAIL, {"collection": gid("Collection", collection.pk)}
        )
    )["data"]["wsmKitConfig"]
    by_id = get_graphql_content(
        staff_api_client.post_graphql(DETAIL, {"id": gid("WsmKitConfig", kit.pk)})
    )["data"]["wsmKitConfig"]

    assert by_collection == by_id
    assert by_collection["discountKind"] == "FIXED"
    assert len(by_collection["members"]) == 2


def test_the_list_filters_by_active_collection_and_search(
    staff_api_client,
    permission_manage_products,
    collection,
    published_collection,
    product_list,
):
    # Named apart on purpose: the search filter is a substring match, so two
    # fixture collections whose names share a prefix would pass this test for
    # the wrong reason.
    collection.name = "Billet Cover Kit"
    collection.save(update_fields=["name"])
    make_kit(collection, product_list)
    make_kit(published_collection, product_list[2:], active=False)
    staff_api_client.user.user_permissions.add(permission_manage_products)

    def listed(filter_):
        content = get_graphql_content(
            staff_api_client.post_graphql(LIST, {"filter": filter_})
        )
        return [
            edge["node"]["collection"]["name"]
            for edge in content["data"]["wsmKitConfigs"]["edges"]
        ]

    assert len(listed({})) == 2
    assert listed({"active": False}) == [published_collection.name]
    assert listed({"collection": gid("Collection", collection.pk)}) == [
        "Billet Cover Kit"
    ]
    assert listed({"search": "Billet"}) == ["Billet Cover Kit"]


def test_the_currency_code_comes_off_the_members_own_listing(
    staff_api_client, permission_manage_products, collection, product_list, channel_USD
):
    """The Dashboard labels a money input without a second round trip."""
    staff_api_client.user.user_permissions.add(permission_manage_products)

    created = get_graphql_content(
        staff_api_client.post_graphql(
            CREATE, {"input": create_input(collection, product_list)}
        )
    )["data"]["wsmKitConfigCreate"]["kit"]

    assert created["currencyCode"] == channel_USD.currency_code
