# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Slots over GraphQL: authoring the roles, and resolving them for a vehicle.

Additive on purpose. `WsmKitConfig`, `WsmKitMember` and every mutation name are
unchanged, because those names are the contract the Dashboard, the storefront
and the stage console rows are written against; what the schema gains is a
`slots` list, a `slot` on a member, a slot input on the two kit mutations, and
one new query.

`wsmContainerResolve` is STAFF-ONLY on purpose. The shopper path does not come
through here: the storefront asks the search engine directly over a public,
CDN-cached GET and groups the answer against the Saleor payload it already
holds. This field is the merchant-side preview, so it inherits the same
MANAGE_PRODUCTS gate as every other container field rather than opening a
second, unauthenticated door onto the same data.
"""

from decimal import Decimal

import graphene
import pytest

from .....graphql.tests.utils import assert_no_permission, get_graphql_content
from ....containers import pricing, resolve
from ....containers.models import ContainerSlot, KitConfig, KitMember
from ....containers.tests.test_resolve import RZR_900, FakeEngine, document

pytestmark = pytest.mark.django_db


CREATE = """
    mutation Create($input: WsmKitConfigCreateInput!) {
      wsmKitConfigCreate(input: $input) {
        kit {
          id
          brand
          published
          isPublished
          missMessage
          slots {
            id label quantity required sortOrder axes partitioningAxis
            missMessage drills
            sourceCollection { id }
            candidates { id variant { sku } }
          }
          members { id variant { sku } slot { label } }
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
          slots { id label sortOrder candidates { id variant { sku } } }
          members { id variant { sku } slot { label } }
        }
        errors { field code message }
      }
    }
"""

RESOLVE = """
    query Resolve($collection: ID!, $fitmentPairs: String) {
      wsmContainerResolve(collection: $collection, fitmentPairs: $fitmentPairs) {
        vehicle
        refused
        engineCalls
        refusal { code message slotLabel }
        kit { id }
        slots {
          choose
          excluded
          slot { label partitioningAxis drills }
          candidates { fitment quantity productId variant { sku } member { id } }
          selected { variant { sku } }
        }
      }
    }
"""


def collection_id(collection):
    return graphene.Node.to_global_id("Collection", collection.pk)


def variant_id(product):
    return graphene.Node.to_global_id("ProductVariant", product.variants.first().pk)


@pytest.fixture
def staff(staff_api_client, permission_manage_products):
    staff_api_client.user.user_permissions.add(permission_manage_products)
    return staff_api_client


@pytest.fixture
def belt_saver(collection, product_list):
    """The stage container as a merchant would author it: one required role."""
    collection.products.add(*product_list)
    kit = KitConfig.objects.create(
        collection=collection,
        discount_kind=pricing.PERCENT,
        discount_amount=Decimal(10),
        miss_message="Ask us about a clutch kit for your machine.",
    )
    slot = ContainerSlot.objects.create(
        kit=kit, label="Clutch weight", required=True, sort_order=0
    )
    for order, product in enumerate(product_list):
        KitMember.objects.create(
            kit=kit,
            slot=slot,
            variant=product.variants.first(),
            quantity=1,
            sort_order=order,
        )
    return kit


# --- authoring ----------------------------------------------------------------


def test_a_container_is_authored_with_its_slots_and_its_parts_in_one_call(
    staff, collection, product_list
):
    response = staff.post_graphql(
        CREATE,
        {
            "input": {
                "collection": collection_id(collection),
                "brand": "Team Alba",
                "published": True,
                "missMessage": "Nothing for that machine yet.",
                "slots": [
                    {
                        "key": "weight",
                        "label": "Clutch weight",
                        "required": True,
                        "sortOrder": 0,
                        "axes": ["color"],
                        "partitioningAxis": "color",
                        "missMessage": "No weight for that machine.",
                    },
                    {
                        "key": "gauge",
                        "label": "Gauge",
                        "required": False,
                        "sortOrder": 1,
                    },
                ],
                "members": [
                    {
                        "key": "a",
                        "variant": variant_id(product_list[0]),
                        "slotKey": "weight",
                    },
                    {
                        "key": "b",
                        "variant": variant_id(product_list[1]),
                        "slotKey": "weight",
                    },
                    {
                        "key": "c",
                        "variant": variant_id(product_list[2]),
                        "slotKey": "gauge",
                    },
                ],
            }
        },
    )

    kit = get_graphql_content(response)["data"]["wsmKitConfigCreate"]["kit"]
    assert kit["brand"] == "Team Alba"
    assert kit["published"] is True
    assert kit["isPublished"] is True
    assert [slot["label"] for slot in kit["slots"]] == ["Clutch weight", "Gauge"]
    assert kit["slots"][0]["drills"] is True
    assert kit["slots"][1]["drills"] is False
    assert [c["variant"]["sku"] for c in kit["slots"][0]["candidates"]] == [
        product_list[0].variants.first().sku,
        product_list[1].variants.first().sku,
    ]
    assert [m["slot"]["label"] for m in kit["members"]] == [
        "Clutch weight",
        "Clutch weight",
        "Gauge",
    ]


def test_a_slot_can_point_at_another_containers_collection(
    staff, collection, collection_list, product_list
):
    """A kit could have several series in it, by reference and never by copy."""
    series = collection_list[0]

    response = staff.post_graphql(
        CREATE,
        {
            "input": {
                "collection": collection_id(collection),
                "slots": [
                    {
                        "key": "tuner",
                        "label": "Tuner",
                        "sourceCollection": collection_id(series),
                    }
                ],
            }
        },
    )

    kit = get_graphql_content(response)["data"]["wsmKitConfigCreate"]["kit"]
    assert kit["slots"][0]["sourceCollection"]["id"] == collection_id(series)
    assert kit["slots"][0]["candidates"] == []


def test_two_slots_with_the_same_name_are_refused(staff, collection):
    response = staff.post_graphql(
        CREATE,
        {
            "input": {
                "collection": collection_id(collection),
                "slots": [{"label": "Gauge"}, {"label": "gauge"}],
            }
        },
    )

    errors = get_graphql_content(response)["data"]["wsmKitConfigCreate"]["errors"]
    assert [e["code"] for e in errors] == ["DUPLICATE_SLOT_LABEL"]
    assert errors[0]["field"] == "slots.1.label"
    assert not ContainerSlot.objects.exists()


def test_a_part_naming_a_slot_the_container_does_not_have_is_refused(
    staff, collection, product_list
):
    response = staff.post_graphql(
        CREATE,
        {
            "input": {
                "collection": collection_id(collection),
                "slots": [{"key": "gauge", "label": "Gauge"}],
                "members": [
                    {"variant": variant_id(product_list[0]), "slotKey": "exhaust"}
                ],
            }
        },
    )

    errors = get_graphql_content(response)["data"]["wsmKitConfigCreate"]["errors"]
    assert [e["code"] for e in errors] == ["SLOT_NOT_IN_CONTAINER"]
    assert not KitConfig.objects.exists()


def test_a_partitioning_axis_the_slot_never_asks_about_is_refused(staff, collection):
    response = staff.post_graphql(
        CREATE,
        {
            "input": {
                "collection": collection_id(collection),
                "slots": [
                    {"label": "Gauge", "axes": ["color"], "partitioningAxis": "finish"}
                ],
            }
        },
    )

    errors = get_graphql_content(response)["data"]["wsmKitConfigCreate"]["errors"]
    assert [e["code"] for e in errors] == ["AXIS_NOT_IN_AXES"]


def test_dropping_a_slot_that_still_holds_parts_is_refused_not_silent(
    staff, belt_saver
):
    """The FK cascades. A merchant editing only the slot list is never told after."""
    response = staff.post_graphql(
        UPDATE,
        {
            "id": graphene.Node.to_global_id("WsmKitConfig", belt_saver.pk),
            "input": {"slots": [{"label": "Something else"}]},
        },
    )

    errors = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]["errors"]
    assert [e["code"] for e in errors] == ["SLOT_STILL_HAS_CANDIDATES"]
    assert "Clutch weight" in errors[0]["message"]
    assert belt_saver.members.count() == 3


def test_dropping_a_slot_and_its_parts_together_is_allowed(staff, belt_saver):
    response = staff.post_graphql(
        UPDATE,
        {
            "id": graphene.Node.to_global_id("WsmKitConfig", belt_saver.pk),
            "input": {"slots": [{"label": "Something else"}], "members": []},
        },
    )

    kit = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]["kit"]
    assert [slot["label"] for slot in kit["slots"]] == ["Something else"]
    assert kit["members"] == []


def test_a_slot_is_edited_in_place_by_id(staff, belt_saver, product_list):
    stored = belt_saver.slots.first()

    response = staff.post_graphql(
        UPDATE,
        {
            "id": graphene.Node.to_global_id("WsmKitConfig", belt_saver.pk),
            "input": {
                "slots": [
                    {
                        "id": graphene.Node.to_global_id("WsmContainerSlot", stored.pk),
                        "label": "Clutch weight",
                        "sortOrder": 7,
                    }
                ],
                "members": [
                    {
                        "variant": variant_id(product_list[0]),
                        "slotId": graphene.Node.to_global_id(
                            "WsmContainerSlot", stored.pk
                        ),
                    }
                ],
            },
        },
    )

    kit = get_graphql_content(response)["data"]["wsmKitConfigUpdate"]["kit"]
    assert len(kit["slots"]) == 1
    assert kit["slots"][0]["id"] == graphene.Node.to_global_id(
        "WsmContainerSlot", stored.pk
    )
    assert kit["slots"][0]["sortOrder"] == 7
    assert ContainerSlot.objects.count() == 1


def test_authoring_slots_needs_manage_products(
    staff_api_client, collection, product_list
):
    response = staff_api_client.post_graphql(
        CREATE,
        {
            "input": {
                "collection": collection_id(collection),
                "slots": [{"label": "Gauge"}],
            }
        },
    )

    assert_no_permission(response)


# --- resolving ----------------------------------------------------------------


@pytest.fixture
def stage_engine(product_list, settings, monkeypatch):
    settings.WSM_SEARCH_ENGINE_URL = "https://search.tonneauoutlaw.test"
    xp900, xp1000, gauge = product_list
    engine = FakeEngine(
        by_vehicle={RZR_900: [document(xp900)]},
        no_vehicle=[
            document(xp900),
            document(xp1000),
            document(gauge, fitment_unknown=True),
        ],
    )
    monkeypatch.setattr(resolve.requests, "get", engine)
    return engine


def test_a_vehicle_resolves_the_container_to_what_fits_it(
    staff, belt_saver, stage_engine, product_list
):
    response = staff.post_graphql(
        RESOLVE,
        {"collection": collection_id(belt_saver.collection), "fitmentPairs": RZR_900},
    )

    resolution = get_graphql_content(response)["data"]["wsmContainerResolve"]
    assert resolution["refused"] is False
    assert resolution["vehicle"] == RZR_900
    assert resolution["engineCalls"] == 2
    [slot] = resolution["slots"]
    assert slot["slot"]["label"] == "Clutch weight"
    assert slot["excluded"] == 1
    assert [(c["variant"]["sku"], c["fitment"]) for c in slot["candidates"]] == [
        (product_list[0].variants.first().sku, "FITS"),
        (product_list[2].variants.first().sku, "UNIVERSAL"),
    ]
    assert all(c["member"] for c in slot["candidates"])
    assert len(slot["selected"]) == 2


def test_a_vehicle_nothing_fits_returns_the_refusal_not_an_error(
    staff, belt_saver, product_list, settings, monkeypatch
):
    settings.WSM_SEARCH_ENGINE_URL = "https://search.tonneauoutlaw.test"
    engine = FakeEngine(
        by_vehicle={RZR_900: []},
        no_vehicle=[document(p) for p in product_list],
    )
    monkeypatch.setattr(resolve.requests, "get", engine)

    response = staff.post_graphql(
        RESOLVE,
        {"collection": collection_id(belt_saver.collection), "fitmentPairs": RZR_900},
    )

    resolution = get_graphql_content(response)["data"]["wsmContainerResolve"]
    assert resolution["refused"] is True
    assert resolution["refusal"]["code"] == "SLOT_HAS_NO_FIT"
    assert resolution["refusal"]["slotLabel"] == "Clutch weight"
    assert (
        resolution["refusal"]["message"]
        == "Ask us about a clutch kit for your machine."
    )
    assert resolution["slots"] == []


def test_no_vehicle_resolves_with_no_engine_call_at_all(
    staff, belt_saver, stage_engine
):
    response = staff.post_graphql(
        RESOLVE, {"collection": collection_id(belt_saver.collection)}
    )

    resolution = get_graphql_content(response)["data"]["wsmContainerResolve"]
    assert resolution["engineCalls"] == 0
    assert stage_engine.calls == []
    assert {c["fitment"] for c in resolution["slots"][0]["candidates"]} == {
        "UNFILTERED"
    }


def test_an_unreachable_engine_refuses_rather_than_showing_everything(
    staff, belt_saver, settings, monkeypatch
):
    import requests

    settings.WSM_SEARCH_ENGINE_URL = "https://search.tonneauoutlaw.test"
    monkeypatch.setattr(
        resolve.requests,
        "get",
        FakeEngine(by_vehicle={}, no_vehicle=[], fail=requests.Timeout("slow")),
    )

    response = staff.post_graphql(
        RESOLVE,
        {"collection": collection_id(belt_saver.collection), "fitmentPairs": RZR_900},
    )

    resolution = get_graphql_content(response)["data"]["wsmContainerResolve"]
    assert resolution["refusal"]["code"] == "ENGINE_UNAVAILABLE"
    assert resolution["slots"] == []


def test_a_collection_that_is_not_a_container_refuses(staff, collection):
    response = staff.post_graphql(
        RESOLVE, {"collection": collection_id(collection), "fitmentPairs": RZR_900}
    )

    resolution = get_graphql_content(response)["data"]["wsmContainerResolve"]
    assert resolution["refusal"]["code"] == "NOT_CONFIGURED"
    assert resolution["kit"] is None


def test_resolving_needs_manage_products(staff_api_client, belt_saver):
    """Staff-only: the shopper path is the engine's public GET, not this field."""
    response = staff_api_client.post_graphql(
        RESOLVE,
        {"collection": collection_id(belt_saver.collection), "fitmentPairs": RZR_900},
    )

    assert_no_permission(response)


# --- the channel a shopper is asking in ---------------------------------------

PRICED_RESOLVE = """
    query Resolve($collection: ID!, $fitmentPairs: String, $channel: String) {
      wsmContainerResolve(
        collection: $collection, fitmentPairs: $fitmentPairs, channel: $channel
      ) {
        slots {
          candidates {
            variant { sku pricing { price { gross { amount currency } } } }
          }
          selected { variant { pricing { price { gross { amount } } } } }
        }
        kit { members { variant { pricing { price { gross { amount } } } } } }
      }
    }
"""

PRICED_SLOTS = """
    query Slots($collection: ID!, $channel: String) {
      wsmKitConfig(collection: $collection, channel: $channel) {
        slots {
          candidates {
            variant { pricing { price { gross { amount } } } }
          }
        }
      }
    }
"""


def test_a_resolved_candidate_is_priced_in_the_channel_it_was_asked_in(
    staff, belt_saver, stage_engine, channel_USD
):
    """A candidate with no price cannot be bought, which is the whole page.

    Stock's `ProductVariant.pricing` returns null without a channel on its
    `ChannelContext` (`graphql/product/types/products.py:700`), so a resolution
    that names no channel hands a storefront an assortment it cannot put a
    number against. The channel travels from this field's own argument, exactly
    as it does on stock's `product(channel:)`.
    """
    response = staff.post_graphql(
        PRICED_RESOLVE,
        {
            "collection": collection_id(belt_saver.collection),
            "fitmentPairs": RZR_900,
            "channel": channel_USD.slug,
        },
    )

    resolution = get_graphql_content(response)["data"]["wsmContainerResolve"]
    [slot] = resolution["slots"]
    assert [
        candidate["variant"]["pricing"]["price"]["gross"]["amount"]
        for candidate in slot["candidates"]
    ] == [10.0, 30.0]
    assert {
        candidate["variant"]["pricing"]["price"]["gross"]["currency"]
        for candidate in slot["candidates"]
    } == {"USD"}
    # The picks are what goes in the cart, and the members are what the kit is
    # made of: both reach the same leaf, so both are priced or neither is.
    assert all(candidate["variant"]["pricing"] for candidate in slot["selected"])
    assert all(member["variant"]["pricing"] for member in resolution["kit"]["members"])


def test_a_slot_candidate_is_priced_in_the_channel_the_kit_was_read_in(
    staff, belt_saver, channel_USD
):
    """The storefront reads its cards through `wsmKitConfig`, not the resolve.

    Same defect, same fix, one hop shorter: the slug travels kit -> slot ->
    candidate -> variant rather than resolution -> candidate -> variant.
    """
    response = staff.post_graphql(
        PRICED_SLOTS,
        {
            "collection": collection_id(belt_saver.collection),
            "channel": channel_USD.slug,
        },
    )

    [slot] = get_graphql_content(response)["data"]["wsmKitConfig"]["slots"]
    assert [
        candidate["variant"]["pricing"]["price"]["gross"]["amount"]
        for candidate in slot["candidates"]
    ] == [10.0, 20.0, 30.0]


def test_naming_no_channel_prices_nothing_and_refuses_nothing(
    staff, belt_saver, stage_engine
):
    """Omitted is the merchant preview this field shipped as, unchanged.

    A Dashboard screen is not shopping in a channel, so no channel is not an
    error: the assortment still answers and the money is simply absent, which is
    fail-SAFE. A storefront that forgets the argument shows no price rather than
    the wrong one.
    """
    response = staff.post_graphql(
        PRICED_RESOLVE,
        {"collection": collection_id(belt_saver.collection), "fitmentPairs": RZR_900},
    )

    [slot] = get_graphql_content(response)["data"]["wsmContainerResolve"]["slots"]
    assert len(slot["candidates"]) == 2
    assert all(
        candidate["variant"]["pricing"] is None for candidate in slot["candidates"]
    )
