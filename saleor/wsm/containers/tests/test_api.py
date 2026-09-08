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
