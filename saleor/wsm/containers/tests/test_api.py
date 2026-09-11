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

from saleor.wsm.containers import pricing, resolve
from saleor.wsm.containers.models import ContainerSlot, KitConfig, KitMember
from saleor.wsm.tests import DEALER_HEADERS

from .test_resolve import RZR_900, FakeEngine, document

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


def post_kit(
    client, checkout, collection_id, quantity=1, customer=None, variant_ids=None
):
    body = {
        "checkoutId": gid("Checkout", checkout.token),
        "collectionId": gid("Collection", collection_id),
        "quantity": quantity,
    }
    if customer is not None:
        body["customerId"] = gid("User", customer.pk)
    if variant_ids is not None:
        # What the kit page posts on every call: the shopper's own picks, as the
        # same global ids this endpoint answers with.
        body["variantIds"] = [gid("ProductVariant", pk) for pk in variant_ids]
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


def test_a_real_dealer_tier_beats_the_kit_discount(
    client, checkout, kit, customer_user
):
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
    assert (
        post_kit(
            client, checkout, kit.collection_id, customer=customer_user
        ).status_code
        == 200
    )
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
    """Nothing re-derived a member that took no tier, so a stale cart kept a discount the merchant had already changed, for the life of that cart."""
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
            # Both columns, because the promotion task keeps them in step and the
            # fork charges from the discounted one: a fixture that moves only the
            # list price describes a listing production never holds.
            price_amount=Decimal(price),
            discounted_price_amount=Decimal(price),
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


def test_the_acceptance_kit_is_9358_19_at_retail(
    client, checkout, bakeoff_kit, product_list
):
    """B4, retail half: the number the bake-off walk pins."""
    response = post_kit(client, checkout, bakeoff_kit.collection_id)

    assert response.status_code == 200
    assert response.json()["kitTotal"] == "9358.19"
    units = {line.variant_id: line.price_override for line in checkout.lines.all()}
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
    units = {line.variant_id: line.price_override for line in checkout.lines.all()}
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


def test_a_tax_exempt_dealers_kit_lands_in_a_cart_that_owes_no_tax(
    client, checkout, kit, customer_user
):
    """Same rule as the configured line, on the other route that prices a buyer.

    A kit is what this shopper is buying, so it is where their exemption has to
    land; see `saleor/wsm/dealer/tax.py` for what is allowed to write the flag.
    """
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup

    group = DealerGroup.objects.create(code="tier-1", name="Tier 1")
    DealerCustomer.objects.create(user=customer_user, group=group, tax_exempt=True)

    response = post_kit(client, checkout, kit.collection_id, customer=customer_user)

    assert response.status_code == 200, response.content
    checkout.refresh_from_db()
    assert checkout.tax_exemption is True


# --- cross-member rules -------------------------------------------------------
#
# The kit adds its members together, so a rule is broken by the kit the merchant
# built rather than by a shopper's typing. That is the same refusal either way:
# money is never taken on a combination the merchant said cannot be sold, and
# the shopper reads the merchant's own sentence rather than a code.


def rule(kit, kind, subject, targets, message):
    from saleor.wsm.containers.models import KitMemberRule

    row = KitMemberRule.objects.create(
        kit=kit, subject=subject, kind=kind, message=message
    )
    row.targets.set(targets)
    return row


def test_a_kit_that_breaks_its_own_rule_is_refused_in_the_merchants_words(
    client, checkout, kit
):
    from saleor.wsm.containers.models import EXCLUDES, RULE_VIOLATION_CODE

    members = list(kit.members.order_by("sort_order"))
    rule(
        kit,
        EXCLUDES,
        members[0],
        [members[2]],
        "The billet cover cannot be fitted with the OEM tensioner.",
    )

    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 422
    payload = response.json()
    assert payload["violations"] == [
        "The billet cover cannot be fitted with the OEM tensioner."
    ]
    assert payload["code"] == RULE_VIOLATION_CODE
    assert payload["rules"] == [
        {
            "kind": EXCLUDES,
            "message": "The billet cover cannot be fitted with the OEM tensioner.",
            "subject": gid("ProductVariant", members[0].variant_id),
            "targets": [gid("ProductVariant", members[2].variant_id)],
            "variantIds": [
                gid("ProductVariant", members[0].variant_id),
                gid("ProductVariant", members[2].variant_id),
            ],
        }
    ]
    # Refused, never re-priced: nothing reached the checkout.
    assert checkout.lines.count() == 0


def test_a_satisfied_rule_lets_the_kit_through_and_says_what_it_was(
    client, checkout, kit
):
    from saleor.wsm.containers.models import REQUIRES_ONE_OF

    members = list(kit.members.order_by("sort_order"))
    rule(
        kit,
        REQUIRES_ONE_OF,
        members[0],
        [members[1]],
        "Requires Manual or HD Tensioner.",
    )

    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 200
    payload = response.json()
    assert payload["kitTotal"] == "54.00"
    assert checkout.lines.count() == 3
    assert payload["rules"] == [
        {
            "kind": REQUIRES_ONE_OF,
            "message": "Requires Manual or HD Tensioner.",
            "subject": gid("ProductVariant", members[0].variant_id),
            "targets": [gid("ProductVariant", members[1].variant_id)],
            "variantIds": [
                gid("ProductVariant", members[0].variant_id),
                gid("ProductVariant", members[1].variant_id),
            ],
        }
    ]


def test_a_rule_about_a_part_that_is_not_in_the_kit_says_nothing(client, checkout, kit):
    """The subject decides whether a rule is even asked. No subject, no rule."""
    from saleor.wsm.containers.models import EXCLUDES, evaluate_rules

    members = list(kit.members.order_by("sort_order"))
    row = rule(kit, EXCLUDES, members[0], [members[2]], "never sold together")

    held, broken = evaluate_rules(kit, [members[1].variant_id])
    assert [rule_row.pk for rule_row in held] == [row.pk]
    assert broken == []
    assert row.broken_by([m.variant_id for m in members]) is True


def test_a_kit_with_no_rules_is_unchanged(client, checkout, kit):
    """The whole feature costs a kit that carries no rules one query and no shape."""
    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 200
    assert response.json()["rules"] == []
    assert checkout.lines.count() == 3


# --- charges on a kit member --------------------------------------------------
#
# A fee is attached to a PRODUCT, so a kit that contains that product owes it
# exactly as the configured-line path does. Demo 4, measured 2026-09-09: the kit
# route never looked at Fee, so a core deposit that the same product charges
# through the PDP was simply not taken.


@pytest.fixture
def fee_kit(collection, product_list, channel_USD):
    """3,680.85 of parts, 10 percent off, one part carrying a 100.00 deposit.

    The demo-4 arithmetic: 368.09 off the MEMBERS (10 percent, half-up), the
    deposit whole, 3,412.76 to pay.
    """
    from saleor.wsm.compose import pricing as compose_pricing
    from saleor.wsm.compose.models import Fee

    body, core = product_list[0], product_list[1]
    collection.products.add(body, core)
    for product, price in ((body, "3180.85"), (core, "500.00")):
        product.variants.first().channel_listings.filter(channel=channel_USD).update(
            # Both columns, because the promotion task keeps them in step and the
            # fork charges from the discounted one: a fixture that moves only the
            # list price describes a listing production never holds.
            price_amount=Decimal(price),
            discounted_price_amount=Decimal(price),
        )
    kit = KitConfig.objects.create(
        collection=collection,
        discount_kind=pricing.PERCENT,
        discount_amount=Decimal(10),
    )
    for order, product in enumerate((body, core)):
        KitMember.objects.create(
            kit=kit, variant=product.variants.first(), quantity=1, sort_order=order
        )
    Fee.objects.create(
        product=core,
        label="Core deposit, refundable",
        sku="170-0565A-CORE",
        basis=compose_pricing.FIXED,
        amount=Decimal("100.00"),
        apply_to=compose_pricing.PER_UNIT,
        required=True,
    )
    return kit


def test_a_kit_member_carrying_a_fee_lands_with_its_own_deposit_line(
    client, checkout, fee_kit, product_list
):
    response = post_kit(client, checkout, fee_kit.collection_id)

    assert response.status_code == 200, response.content
    payload = response.json()
    # The kit total is the MEMBERS. The deposit is not part of what the kit
    # discount was computed on and is not discounted by it.
    assert payload["kitTotal"] == "3312.76"
    assert payload["feeTotal"] == "100.00"
    assert [line["unitPrice"] for line in payload["lines"]] == ["2862.76", "450.00"]

    core = product_list[1].variants.first()
    assert len(payload["fees"]) == 1
    charge = payload["fees"][0]
    assert charge["label"] == "Core deposit, refundable"
    # The merchant's own code for the charge, and the whole charge on the line.
    assert charge["sku"] == "170-0565A-CORE"
    assert charge["amount"] == "100.00"
    assert charge["unitPrice"] == "100.00"
    assert charge["quantity"] == 1
    assert charge["parentVariantId"] == gid("ProductVariant", core.pk)
    assert charge["lineId"]

    lines = list(checkout.lines.all())
    assert len(lines) == 3
    assert sum(line.price_override * line.quantity for line in lines) == Decimal(
        "3412.76"
    )
    deposit = checkout.lines.get(variant__sku__startswith="wsm-fee-")
    assert deposit.price_override == Decimal("100.00")
    assert deposit.quantity == 1


def test_the_deposit_line_says_what_it_belongs_to(
    client, checkout, fee_kit, product_list
):
    """The pairing the storefront cart draws the charge under its part with."""
    from saleor.wsm.compose.lines import META_CID, META_FEE, META_PARENT

    assert post_kit(client, checkout, fee_kit.collection_id).status_code == 200

    core = product_list[1].variants.first()
    member = checkout.lines.get(variant_id=core.pk)
    deposit = checkout.lines.get(variant__sku__startswith="wsm-fee-")

    assert deposit.metadata[META_PARENT] == member.metadata[META_CID]
    assert json.loads(deposit.metadata[META_FEE]) == {
        "label": "Core deposit, refundable",
        "apply_to": "unit",
    }
    # Ours to price from, so the copy that decides money is private.
    assert deposit.private_metadata[META_PARENT] == member.private_metadata[META_CID]
    # The charge rides with the kit it came from, so a cart groups it with the kit.
    assert deposit.metadata[pricing.META_GROUP] == member.metadata[pricing.META_GROUP]


def test_a_per_unit_deposit_follows_the_kit_quantity(client, checkout, fee_kit):
    response = post_kit(client, checkout, fee_kit.collection_id, quantity=2)

    assert response.status_code == 200
    payload = response.json()
    assert payload["kitTotal"] == "6625.52"
    assert payload["feeTotal"] == "200.00"
    deposit = checkout.lines.get(variant__sku__startswith="wsm-fee-")
    assert deposit.quantity == 2
    assert deposit.price_override == Decimal("100.00")


def test_the_deposit_survives_the_next_cart_read(client, checkout, fee_kit):
    """Every cart read re-derives the kit. The charge is re-derived with it."""
    assert post_kit(client, checkout, fee_kit.collection_id).status_code == 200

    after = recalculate(checkout)

    deposit = checkout.lines.get(variant__sku__startswith="wsm-fee-")
    assert after[deposit.variant_id] == Decimal("100.00")
    assert checkout.lines.count() == 3
    assert sum(
        line.price_override * line.quantity for line in checkout.lines.all()
    ) == Decimal("3412.76")


def test_a_kit_with_no_fees_gains_no_lines(client, checkout, kit):
    """The whole feature is invisible to a kit whose parts carry no charges."""
    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 200
    payload = response.json()
    assert payload["feeTotal"] == "0.00"
    assert payload["fees"] == []
    assert checkout.lines.count() == 3


# --- the shopper's picks, and the saving as its own row -----------------------
#
# The kit page posts `variantIds` on every call (wsm-storefront demo/tno,
# src/lib/kitLine.ts): a kit that offers a manual or an HD tensioner is bought
# as the parts the shopper chose. Measured 2026-09-09: the endpoint ignored them
# and priced the collection's own members, so a substitution was never priced.


def test_the_picked_parts_are_the_kit_that_is_priced(
    client, checkout, kit, product_list
):
    """The discount prorates over what is being bought, and over nothing else."""
    picked = [product_list[0].variants.first().pk, product_list[2].variants.first().pk]

    response = post_kit(client, checkout, kit.collection_id, variant_ids=picked)

    assert response.status_code == 200, response.content
    payload = response.json()
    # 10.00 + 30.00 list, 10 percent off, prorated 1.00 / 3.00.
    assert payload["listTotal"] == "40.00"
    assert payload["kitTotal"] == "36.00"
    assert [line["unitPrice"] for line in payload["lines"]] == ["9.00", "27.00"]
    assert {line.variant_id for line in checkout.lines.all()} == set(picked)


def test_a_pick_that_is_not_in_the_kit_is_refused(
    client, checkout, kit, product_list, variant
):
    """Never quietly dropped: pricing a different kit is the whole defect."""
    stranger = variant
    assert stranger.pk not in set(kit.members.values_list("variant_id", flat=True))

    response = post_kit(
        client,
        checkout,
        kit.collection_id,
        variant_ids=[kit.members.first().variant_id, stranger.pk],
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "kit_member_unknown"
    assert payload["variantIds"] == [gid("ProductVariant", stranger.pk)]
    assert checkout.lines.count() == 0


def test_an_empty_pick_list_is_refused(client, checkout, kit):
    response = post_kit(client, checkout, kit.collection_id, variant_ids=[])

    assert response.status_code == 422
    assert response.json()["violations"] == ["choose at least one part of this kit"]
    assert checkout.lines.count() == 0


def test_no_picks_is_the_kit_the_merchant_built(client, checkout, kit):
    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 200
    assert len(response.json()["lines"]) == 3
    assert response.json()["listTotal"] == "60.00"


def test_the_saving_is_its_own_row(client, checkout, kit, collection):
    """The rail prints a discount it was GIVEN, never one it worked out itself."""
    payload = post_kit(client, checkout, kit.collection_id).json()

    assert payload["discount"] == {
        "label": f"{collection.name} bundle discount (10%)",
        "percent": "10",
        "amount": "6.00",
        "listTotal": "60.00",
    }
    assert payload["listTotal"] == "60.00"
    assert payload["kitTotal"] == "54.00"
    assert Decimal(payload["listTotal"]) - Decimal(
        payload["discount"]["amount"]
    ) == Decimal(payload["kitTotal"])


def test_a_flat_saving_quotes_no_percentage(client, checkout, kit, collection):
    """A number the merchant did not type is a number nobody can check."""
    kit.discount_kind = pricing.FIXED
    kit.discount_amount = Decimal("5.00")
    kit.save(update_fields=["discount_kind", "discount_amount"])

    payload = post_kit(client, checkout, kit.collection_id).json()

    assert payload["discount"] == {
        "label": f"{collection.name} bundle discount",
        "percent": None,
        "amount": "5.00",
        "listTotal": "60.00",
    }


def test_the_saving_counts_a_tiered_member_too(client, checkout, kit, customer_user):
    """`amount` is the sum of the per-line spreads, so the rail always adds up.

    The dear member takes a 25.00 tier instead of its kit-discounted 27.00, so
    the saving on the kit is 1.00 + 2.00 + 5.00, not the 6.00 the percentage
    alone would have said.
    """
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup, TierPrice

    group = DealerGroup.objects.create(code="tier-1", name="Tier 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    dear = kit.members.order_by("-sort_order").first().variant
    TierPrice.objects.create(
        variant=dear, group=group, min_quantity=1, amount=Decimal("25.00")
    )

    payload = post_kit(
        client, checkout, kit.collection_id, customer=customer_user
    ).json()

    assert payload["kitTotal"] == "52.00"
    assert payload["discount"]["amount"] == "8.00"
    assert Decimal(payload["listTotal"]) - Decimal(
        payload["discount"]["amount"]
    ) == Decimal(payload["kitTotal"])


def test_a_rule_is_asked_of_the_picks_and_not_of_the_kit(client, checkout, kit):
    """Demo 3, as the shopper meets it: the tensioner they chose is the question."""
    from saleor.wsm.containers.models import REQUIRES_ONE_OF, RULE_VIOLATION_CODE

    members = list(kit.members.order_by("sort_order"))
    rule(
        kit,
        REQUIRES_ONE_OF,
        members[0],
        [members[1]],
        "Requires Manual or HD Tensioner.",
    )

    without = post_kit(
        client,
        checkout,
        kit.collection_id,
        variant_ids=[members[0].variant_id, members[2].variant_id],
    )
    assert without.status_code == 422
    assert without.json()["violations"] == ["Requires Manual or HD Tensioner."]
    assert without.json()["code"] == RULE_VIOLATION_CODE
    assert without.json()["rules"][0]["variantIds"] == [
        gid("ProductVariant", members[0].variant_id),
        gid("ProductVariant", members[1].variant_id),
    ]
    assert checkout.lines.count() == 0

    with_it = post_kit(
        client,
        checkout,
        kit.collection_id,
        variant_ids=[members[0].variant_id, members[1].variant_id],
    )
    assert with_it.status_code == 200
    assert checkout.lines.count() == 2


def test_a_charge_on_a_part_nobody_picked_is_not_taken(
    client, checkout, fee_kit, product_list
):
    """The deposit belongs to the core, so a kit bought without it owes nothing."""
    body = product_list[0].variants.first()

    response = post_kit(client, checkout, fee_kit.collection_id, variant_ids=[body.pk])

    assert response.status_code == 200
    payload = response.json()
    assert payload["fees"] == []
    assert payload["feeTotal"] == "0.00"
    assert checkout.lines.count() == 1


def test_the_picked_kit_is_re_derived_as_the_picked_kit(
    client, checkout, kit, product_list
):
    """The stamp carries the picks, so a cart read cannot re-price the whole kit."""
    picked = [product_list[0].variants.first().pk, product_list[2].variants.first().pk]
    assert (
        post_kit(client, checkout, kit.collection_id, variant_ids=picked).status_code
        == 200
    )
    before = {line.variant_id: line.price_override for line in checkout.lines.all()}

    after = recalculate(checkout)

    assert after == before
    assert set(after) == set(picked)


# --- the member the merchant put on sale ------------------------------------


def test_a_member_on_sale_is_priced_at_the_sale_price(
    client, checkout, kit, product_list
):
    """A kit never charges list for a part its own shop advertises cheaper.

    Measured live 2026-09-09: the Team Alba rebuild kit's 350-96MM-KIT member sat
    at base 2154.00 under a 254.01 catalogue promotion, Saleor's own pricing
    answered 1899.99, and this endpoint charged 2154.00 and billed the kit
    2787.00 instead of 2532.99. The promotion is modelled here exactly as the
    promotion tasks leave it, as `discounted_price_amount` on the listing.

    The cheapest member's discounted price is NULLED on purpose: that is how
    Saleor leaves a listing no price recalculation has reached yet, and the
    fallback there has to be the list price rather than nothing at all.
    """
    cheapest, middle, dearest = (p.variants.first() for p in product_list)
    dearest.channel_listings.filter(channel=checkout.channel).update(
        discounted_price_amount=Decimal("24.00")
    )
    cheapest.channel_listings.filter(channel=checkout.channel).update(
        discounted_price_amount=None
    )

    response = post_kit(client, checkout, kit.collection_id)

    assert response.status_code == 200, response.content
    payload = response.json()
    units = {line["variantId"]: line["unitPrice"] for line in payload["lines"]}
    # 10.00 + 20.00 + 24.00 = 54.00, ten percent off = 5.40, prorated by share
    # as 1.00 / 2.00 / 2.40 with no residue to place.
    assert units[gid("ProductVariant", cheapest.pk)] == "9.00"
    assert units[gid("ProductVariant", middle.pk)] == "18.00"
    assert units[gid("ProductVariant", dearest.pk)] == "21.60"
    assert payload["listTotal"] == "54.00"
    assert payload["discount"]["amount"] == "5.40"
    assert payload["kitTotal"] == "48.60"
    # On the lines, not only in the answer.
    assert checkout.lines.get(variant_id=dearest.pk).price_override == Decimal("21.60")
    # And a cart read re-derives it from the same listing, so it cannot drift
    # back up to list on the next page the shopper loads.
    assert recalculate(checkout)[dearest.pk] == Decimal("21.60")


# --- malformed ids are refused, and the checkout's own user is evidence -----


def post_kit_raw(client, body):
    return client.post(
        KIT_LINE_URL, data=json.dumps(body), content_type="application/json", **HEADERS
    )


@pytest.mark.parametrize(
    "body",
    [
        {"checkoutId": "garbage", "collectionId": gid("Collection", 1)},
        {
            "checkoutId": gid("Checkout", "not-a-uuid"),
            "collectionId": gid("Collection", 1),
        },
        {
            "checkoutId": gid("Checkout", "11111111-1111-1111-1111-111111111111"),
            "collectionId": gid("Collection", "not-a-number"),
        },
    ],
)
def test_an_id_that_is_not_an_id_is_refused_rather_than_raised(client, body):
    response = post_kit_raw(client, body)

    assert response.status_code == 400, response.content
    assert "malformed" in response.json()["violations"][0]


def test_the_kit_add_refuses_a_buyer_the_checkout_does_not_belong_to(
    client, checkout, kit, customer_user, staff_user, channel_USD
):
    checkout.user = staff_user
    checkout.save(update_fields=["user"])

    response = post_kit_raw(
        client,
        {
            "checkoutId": gid("Checkout", checkout.token),
            "collectionId": gid("Collection", kit.collection_id),
            "quantity": 1,
            "customerId": gid("User", customer_user.pk),
        },
    )

    assert response.status_code == 409, response.content
    assert checkout.lines.count() == 0


# --- a cart read does not pay per kit --------------------------------------


@pytest.fixture
def three_kits(product_list, channel_USD):
    """Three distinct kits, one member each, so the only variable is kit COUNT."""
    from saleor.product.models import Collection

    kits = []
    for index, product in enumerate(product_list):
        collection = Collection.objects.create(
            name=f"Kit {index}", slug=f"kit-count-{index}"
        )
        collection.products.add(product)
        kit = KitConfig.objects.create(
            collection=collection,
            discount_kind=pricing.PERCENT,
            discount_amount=Decimal(10),
        )
        KitMember.objects.create(
            kit=kit, variant=product.variants.first(), quantity=1, sort_order=0
        )
        kits.append(kit)
    return kits


def test_a_cart_read_does_not_buy_three_queries_per_kit(client, checkout, three_kits):
    """MP3 runs on every price recalculation, so this is a CART READ cost.

    Keyed by kit it was the config, its members and their channel listings, once
    per distinct kit on the checkout: measured +3.00 queries per kit, 26 at k=1
    and 32 at k=3. Flat now, three for the whole cart, so the count is the same
    whatever the shopper put in it.
    """
    from django.db import connections
    from django.test.utils import CaptureQueriesContext

    from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
    from saleor.plugins.manager import get_plugins_manager
    from saleor.wsm.reprice import reprice

    counted = {}
    for k in (1, 2, 3):
        checkout.lines.all().delete()
        for kit in three_kits[:k]:
            assert post_kit(client, checkout, kit.collection_id).status_code == 200

        lines, _ = fetch_checkout_lines(checkout)
        manager = get_plugins_manager(allow_replica=False)
        checkout_info = fetch_checkout_info(checkout, lines, manager)
        # The replica alias proxies `default` in tests and SHARES its
        # queries_log, so capturing both would double every count.
        with CaptureQueriesContext(connections["default"]) as captured:
            reprice(checkout_info, lines)
        counted[k] = len(captured.captured_queries)

    assert counted[1] == counted[2] == counted[3], counted


# --- the resolved assortment goes through the SAME kit money, not a second path -


@pytest.fixture
def bakeoff_container(bakeoff_kit):
    """The acceptance kit as a one-slot container, exactly as the backfill makes it.

    No partitioning axis, which is the data that says "take every candidate that
    survives". If that is wrong, these two numbers move.
    """
    slot = ContainerSlot.objects.create(
        kit=bakeoff_kit, label="Included parts", required=True, sort_order=0
    )
    bakeoff_kit.members.update(slot=slot)
    return bakeoff_kit


def test_the_resolver_picks_the_acceptance_kit_and_it_is_still_9358_19(
    client, checkout, bakeoff_container, product_list
):
    """B4 retail, through `resolve` -> `pricing_members`. No vehicle, no engine."""
    resolution = resolve.resolve(bakeoff_container.collection)

    assert resolution.engine_calls == 0
    picks = resolution.selected_variant_ids()
    assert picks == [
        product_list[0].variants.first().pk,
        product_list[1].variants.first().pk,
    ]

    response = post_kit(
        client, checkout, bakeoff_container.collection_id, variant_ids=picks
    )

    assert response.status_code == 200
    assert response.json()["kitTotal"] == "9358.19"


def test_a_vehicle_resolved_assortment_is_still_9358_19_at_retail(
    client, checkout, bakeoff_container, product_list, settings, monkeypatch
):
    """Both members fit the machine, so the vehicle changes the money by nothing."""
    settings.WSM_SEARCH_ENGINE_URL = "https://search.tonneauoutlaw.test"
    members = [product_list[0], product_list[1]]
    engine = FakeEngine(
        by_vehicle={RZR_900: [document(p) for p in members]},
        no_vehicle=[document(p) for p in members],
    )
    monkeypatch.setattr(resolve.requests, "get", engine)

    resolution = resolve.resolve(bakeoff_container.collection, RZR_900)

    assert resolution.engine_calls == 2
    assert [c.fitment for c in resolution.slots[0].candidates] == [
        resolve.FITS,
        resolve.FITS,
    ]
    response = post_kit(
        client,
        checkout,
        bakeoff_container.collection_id,
        variant_ids=resolution.selected_variant_ids(),
    )

    assert response.json()["kitTotal"] == "9358.19"


def test_a_vehicle_resolved_assortment_is_still_9159_10_for_a_tagged_dealer(
    client,
    checkout,
    bakeoff_container,
    product_list,
    customer_user,
    settings,
    monkeypatch,
):
    """B4 dealer half: tiers apply per line because the lines are still real."""
    from saleor.wsm.dealer.models import DealerCustomer, DealerGroup, TierPrice

    group = DealerGroup.objects.create(code="dealer-1", name="Dealer 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    TierPrice.objects.create(
        variant=product_list[0].variants.first(),
        group=group,
        min_quantity=1,
        amount=Decimal("3400.00"),
    )
    settings.WSM_SEARCH_ENGINE_URL = "https://search.tonneauoutlaw.test"
    members = [product_list[0], product_list[1]]
    engine = FakeEngine(
        by_vehicle={RZR_900: [document(p) for p in members]},
        no_vehicle=[document(p) for p in members],
    )
    monkeypatch.setattr(resolve.requests, "get", engine)

    resolution = resolve.resolve(bakeoff_container.collection, RZR_900)
    response = post_kit(
        client,
        checkout,
        bakeoff_container.collection_id,
        customer=customer_user,
        variant_ids=resolution.selected_variant_ids(),
    )

    assert response.status_code == 200
    assert response.json()["kitTotal"] == "9159.10"
