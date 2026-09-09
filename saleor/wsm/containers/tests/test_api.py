# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The kit endpoint, asserted on what the CHECKOUT ends up holding.

`product_list` prices its three members at 10.00, 20.00 and 30.00 in USD, so a
kit of the three at 10 percent off is 60.00 less 6.00 = 54.00, prorated exactly
1.00 / 2.00 / 3.00 with no residue to place.
"""

import base64
import json
from decimal import Decimal

import pytest

from saleor.wsm.containers import pricing
from saleor.wsm.tests import DEALER_HEADERS
from saleor.wsm.containers.models import KitConfig, KitMember

pytestmark = pytest.mark.django_db

KIT_LINE_URL = "/wsm/containers/api/checkout/kit-line"

# The storefront server proves itself with the tenant key (saleor/wsm/http.py).
HEADERS = DEALER_HEADERS


def gid(type_name, pk):
    return base64.b64encode(f"{type_name}:{pk}".encode()).decode()


@pytest.fixture
def kit(collection, product_list):
    collection.products.add(*product_list)
    kit = KitConfig.objects.create(
        collection=collection,
        discount_kind=pricing.PERCENT,
        discount_amount=Decimal(10),
    )
    for order, product in enumerate(product_list):
        KitMember.objects.create(
            kit=kit, variant=product.variants.first(), quantity=1, sort_order=order
        )
    return kit


def post_kit(client, checkout, collection_id, quantity=1, customer=None):
    body = {
        "checkoutId": gid("Checkout", checkout.token),
        "collectionId": gid("Collection", collection_id),
        "quantity": quantity,
    }
    if customer is not None:
        body["customerId"] = gid("User", customer.pk)
    return client.post(
        KIT_LINE_URL,
        data=json.dumps(body),
        content_type="application/json",
        **HEADERS,
    )


def test_kit_explodes_into_one_priced_line_per_member(client, checkout, kit):
    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 200
    payload = response.json()
    assert payload["kitTotal"] == "54.00"
    assert [line["unitPrice"] for line in payload["lines"]] == [
        "9.00",
        "18.00",
        "27.00",
    ]
    assert [line["share"] for line in payload["lines"]] == ["0.17", "0.33", "0.50"]
    assert all(line["lineId"] for line in payload["lines"])

    lines = list(checkout.lines.all())
    assert len(lines) == 3
    assert sorted(line.price_override for line in lines) == [
        Decimal("9.00"),
        Decimal("18.00"),
        Decimal("27.00"),
    ]
    assert {line.price_override_reason for line in lines} == {
        pricing.PRICE_OVERRIDE_REASON
    }
    # One group id, on every line, and it is the one the caller was handed.
    assert {line.metadata[pricing.META_GROUP] for line in lines} == {payload["groupId"]}
    stamps = sorted(
        (json.loads(line.metadata[pricing.META_KIT]) for line in lines),
        key=lambda stamp: stamp["share"],
    )
    assert stamps == [
        {"collection": kit.collection.slug, "share": "0.17"},
        {"collection": kit.collection.slug, "share": "0.33"},
        {"collection": kit.collection.slug, "share": "0.50"},
    ]
    # The lines ARE the kit total: a shopper is charged what the response said.
    assert sum(line.price_override * line.quantity for line in lines) == Decimal(
        "54.00"
    )


def test_kit_quantity_multiplies_the_line_quantities(client, checkout, kit):
    response = post_kit(client, checkout, kit.collection_id, quantity=2)

    assert response.status_code == 200
    assert response.json()["kitTotal"] == "108.00"
    assert {line.quantity for line in checkout.lines.all()} == {2}


def test_unknown_kit_is_404(client, checkout, db):
    response = post_kit(client, checkout, 999999)

    assert response.status_code == 404
    assert response.json()["violations"] == ["unknown kit"]


def test_inactive_kit_is_422(client, checkout, kit):
    kit.active = False
    kit.save(update_fields=["active"])

    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 422
    assert checkout.lines.count() == 0


def test_unknown_checkout_is_404(client, kit, db):
    response = client.post(
        KIT_LINE_URL,
        data=json.dumps(
            {
                "checkoutId": gid("Checkout", "11111111-1111-1111-1111-111111111111"),
                "collectionId": gid("Collection", kit.collection_id),
            }
        ),
        content_type="application/json",
        **HEADERS,
    )

    assert response.status_code == 404


def test_a_dealer_tier_reaches_the_written_line(client, checkout, kit, monkeypatch):
    """The seam wsm.dealer plugs into, proven with a fake before it lands."""
    cheap = kit.members.first().variant_id

    def tier_lookup(variant, user, quantity):
        return Decimal("5.00") if variant.pk == cheap else None

    monkeypatch.setattr(
        "saleor.wsm.containers.views.resolve_tier_lookup", lambda *_: tier_lookup
    )

    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 200
    # 5.00 flat on the tiered member (not 5.00 less its prorated 1.00), the
    # other two still at their kit-discounted retail units.
    assert [line["unitPrice"] for line in response.json()["lines"]] == [
        "5.00",
        "18.00",
        "27.00",
    ]
    assert response.json()["kitTotal"] == "50.00"


def test_a_real_dealer_tier_beats_the_kit_discount(client, checkout, kit, customer_user):
    """The wired seam, with no injection: wsm.dealer answers, better-of decides.

    The 30.00 member is tiered at 25.00, which beats its kit-discounted 27.00,
    so that line takes the tier and the kit discount contributes nothing to it.
    The other two are untouched at 9.00 and 18.00, which is the whole point of
    better of, never both: a dealer never pays more than retail on any line,
    and never collects both reductions on one.
    """
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup, TierPrice

    group = DealerGroup.objects.create(code="tier-1", name="Tier 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    dear = kit.members.order_by("-sort_order").first().variant
    TierPrice.objects.create(
        variant=dear, group=group, min_quantity=1, amount=Decimal("25.00")
    )

    response = post_kit(client, checkout, kit.collection_id, customer=customer_user)

    assert response.status_code == 200
    assert [line["unitPrice"] for line in response.json()["lines"]] == [
        "9.00",
        "18.00",
        "25.00",
    ]
    assert response.json()["kitTotal"] == "52.00"


def test_a_shopper_with_no_dealer_row_gets_the_kit_discount(
    client, checkout, kit, customer_user
):
    """The same wired path, for the customer the merchant never tiered."""
    response = post_kit(client, checkout, kit.collection_id, customer=customer_user)

    assert response.status_code == 200
    assert [line["unitPrice"] for line in response.json()["lines"]] == [
        "9.00",
        "18.00",
        "27.00",
    ]
    assert response.json()["kitTotal"] == "54.00"


def test_a_voucher_does_not_stack_on_a_tiered_kit_member(
    client, checkout, kit, customer_user, voucher_percentage
):
    """Better of, never both, through Saleor's own voucher machinery.

    The same 10 percent code is applied to two of the three member products.
    The retail member takes it; the member that took a dealer tier does not,
    because the line carries the dealer key the no-stacking guard looks for.
    """
    from saleor.discount import VoucherType
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup, TierPrice

    group = DealerGroup.objects.create(code="tier-1", name="Tier 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    members = list(kit.members.order_by("sort_order"))
    cheap, dear = members[0].variant, members[-1].variant
    TierPrice.objects.create(
        variant=dear, group=group, min_quantity=1, amount=Decimal("25.00")
    )
    voucher_percentage.type = VoucherType.SPECIFIC_PRODUCT
    voucher_percentage.save(update_fields=["type"])
    voucher_percentage.products.add(cheap.product, dear.product)

    added = post_kit(client, checkout, kit.collection_id, customer=customer_user)
    assert added.status_code == 200

    line = checkout.lines.get(variant_id=dear.pk)
    assert line.price_override == Decimal("25.00")
    assert "wsm.dealer" in line.private_metadata

    response = client.post(
        "/graphql/",
        data=json.dumps(
            {
                "query": """
                mutation($id: ID!, $code: String!) {
                  checkoutAddPromoCode(id: $id, promoCode: $code) {
                    errors { field message }
                    checkout { lines {
                      variant { id }
                      totalPrice { gross { amount } }
                    } }
                  }
                }""",
                "variables": {
                    "id": gid("Checkout", checkout.token),
                    "code": "saleor",
                },
            }
        ),
        content_type="application/json",
    )

    payload = response.json()["data"]["checkoutAddPromoCode"]
    assert payload["errors"] == []
    totals = {
        line["variant"]["id"]: line["totalPrice"]["gross"]["amount"]
        for line in payload["checkout"]["lines"]
    }
    # The retail member proves the code is live: 9.00 less 10 percent. The
    # tiered member keeps its 25.00 whole; 22.50 here is the stack this exists
    # to refuse.
    assert totals[gid("ProductVariant", cheap.pk)] == 8.10
    assert totals[gid("ProductVariant", dear.pk)] == 25.00


def test_a_tiered_kit_member_is_written_as_a_dealer_line(
    client, checkout, kit, customer_user
):
    """The private stamp and the reason MP1 and a support screen both read."""
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup, TierPrice
    from saleor.wsm.dealer.no_stacking import LINE_METADATA_KEY, PRICE_OVERRIDE_REASON

    group = DealerGroup.objects.create(code="tier-1", name="Tier 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    dear = kit.members.order_by("-sort_order").first().variant
    TierPrice.objects.create(
        variant=dear, group=group, min_quantity=1, amount=Decimal("25.00")
    )

    added = post_kit(client, checkout, kit.collection_id, customer=customer_user)
    assert added.status_code == 200

    tiered = checkout.lines.get(variant_id=dear.pk)
    assert json.loads(tiered.private_metadata[LINE_METADATA_KEY]) == {"group": "tier-1"}
    assert LINE_METADATA_KEY not in tiered.metadata, "a pricing input is never public"
    assert tiered.price_override_reason == PRICE_OVERRIDE_REASON
    # The members that stayed at the kit-discounted retail unit are not dealer
    # lines and a voucher may still reach them.
    retail = checkout.lines.exclude(variant_id=dear.pk)
    assert not any(LINE_METADATA_KEY in line.private_metadata for line in retail)
    assert {line.price_override_reason for line in retail} == {
        pricing.PRICE_OVERRIDE_REASON
    }


def recalculate(checkout):
    """What every cart read does: run the funnel over the checkout's lines."""
    from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
    from saleor.plugins.manager import get_plugins_manager
    from saleor.wsm.reprice import reprice

    lines, _ = fetch_checkout_lines(checkout)
    manager = get_plugins_manager(allow_replica=False)
    reprice(fetch_checkout_info(checkout, lines, manager), lines)
    return {line.variant_id: line.price_override for line in checkout.lines.all()}


def test_a_member_that_loses_its_tier_falls_back_to_the_kit_price_not_to_list(
    client, checkout, kit, customer_user
):
    """The overcharge MP3 used to write on the first recalculation after a change.

    A kit member that took a dealer tier carries the dealer stamp, and MP3 read
    that stamp and re-ran the plain per-variant LADDER over the line: it knew
    nothing about the kit the line came from. So when the merchant withdrew the
    tier, the line did not fall back to the kit price it was still entitled to.
    It fell back to LIST, and the shopper paid the whole kit discount back on
    that member without touching their cart.
    """
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup, TierPrice

    group = DealerGroup.objects.create(code="tier-1", name="Tier 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    dear = kit.members.order_by("-sort_order").first().variant
    TierPrice.objects.create(
        variant=dear, group=group, min_quantity=1, amount=Decimal("25.00")
    )
    assert post_kit(client, checkout, kit.collection_id, customer=customer_user).status_code == 200
    on_tier = checkout.lines.get(variant_id=dear.pk).price_override
    assert on_tier == Decimal("25.00")

    TierPrice.objects.all().delete()
    after = recalculate(checkout)

    kit_price = pricing.price_kit(
        kit.pricing_members(checkout.channel), kit.discount_kind, kit.discount_amount
    )
    entitled = {
        row.member.variant.pk: Decimal(row.unit_cents) / 100 for row in kit_price.lines
    }
    assert after[dear.pk] == entitled[dear.pk]
    assert after[dear.pk] < dear.channel_listings.get().price_amount, "never list"


def test_the_kit_money_is_re_derived_on_every_read(client, checkout, kit):
    """Nothing re-derived a member that took no tier, so a stale cart kept a
    discount the merchant had already changed, for the life of that cart.
    """
    assert post_kit(client, checkout, kit.collection_id).status_code == 200
    before = {line.variant_id: line.price_override for line in checkout.lines.all()}

    kit.discount_amount = Decimal(50)
    kit.save(update_fields=["discount_amount"])
    after = recalculate(checkout)

    kit_price = pricing.price_kit(
        kit.pricing_members(checkout.channel), kit.discount_kind, kit.discount_amount
    )
    for row in kit_price.lines:
        assert after[row.member.variant.pk] == Decimal(row.unit_cents) / 100
        assert after[row.member.variant.pk] < before[row.member.variant.pk]


@pytest.fixture
def bakeoff_kit(collection, product_list, channel_USD):
    """The two bake-off kits as one container: 3998.99 + 6399.00, 10 percent off.

    List 10397.99, discount 1039.80, prorated across the two members; the single
    residue cent falls to the larger shortfall, which is the Stage 2 member.
    """
    stage_2, stage_3 = product_list[0], product_list[1]
    collection.products.add(stage_2, stage_3)
    for product, price in ((stage_2, "3998.99"), (stage_3, "6399.00")):
        product.variants.first().channel_listings.filter(channel=channel_USD).update(
            price_amount=Decimal(price)
        )
    kit = KitConfig.objects.create(
        collection=collection,
        discount_kind=pricing.PERCENT,
        discount_amount=Decimal(10),
    )
    for order, product in enumerate((stage_2, stage_3)):
        KitMember.objects.create(
            kit=kit, variant=product.variants.first(), quantity=1, sort_order=order
        )
    return kit


def test_the_acceptance_kit_is_9358_19_at_retail(client, checkout, bakeoff_kit, product_list):
    """B4, retail half: the number the bake-off walk pins."""
    response = post_kit(client, checkout, bakeoff_kit.collection_id)

    assert response.status_code == 200
    assert response.json()["kitTotal"] == "9358.19"
    units = {
        line.variant_id: line.price_override for line in checkout.lines.all()
    }
    assert units[product_list[0].variants.first().pk] == Decimal("3599.09")
    assert units[product_list[1].variants.first().pk] == Decimal("5759.10")


def test_the_acceptance_kit_is_9159_10_for_a_dealer_on_a_3400_break(
    client, checkout, bakeoff_kit, product_list, customer_user
):
    """B4, dealer half: better-of per member line, never both."""
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup, TierPrice

    group = DealerGroup.objects.create(code="dealer-1", name="Dealer 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    stage_2 = product_list[0].variants.first()
    TierPrice.objects.create(
        variant=stage_2, group=group, min_quantity=1, amount=Decimal("3400.00")
    )

    response = post_kit(
        client, checkout, bakeoff_kit.collection_id, customer=customer_user
    )

    assert response.status_code == 200
    assert response.json()["kitTotal"] == "9159.10"
    units = {
        line.variant_id: line.price_override for line in checkout.lines.all()
    }
    # The tier beats the kit-discounted 3599.09, so the member pays the break.
    assert units[stage_2.pk] == Decimal("3400.00")
    # The member with no break keeps its kit price. Never both.
    assert units[product_list[1].variants.first().pk] == Decimal("5759.10")



# --- finding 8: the same two holes on the kit write path ---------------------


def test_a_kit_quantity_that_is_not_a_number_is_refused(client, checkout, kit):
    response = client.post(
        KIT_LINE_URL,
        data=json.dumps(
            {
                "checkoutId": gid("Checkout", checkout.token),
                "collectionId": gid("Collection", kit.collection_id),
                "quantity": "x",
            }
        ),
        content_type="application/json",
        **HEADERS,
    )

    assert response.status_code == 422
    assert response.json() == {"violations": ["quantity must be a whole number"]}


def test_a_kit_member_not_available_for_purchase_is_refused(client, checkout, kit):
    member = kit.members.first()
    member.variant.product.channel_listings.filter(channel=checkout.channel).update(
        available_for_purchase_at=None
    )

    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 422
    assert checkout.lines.count() == 0
