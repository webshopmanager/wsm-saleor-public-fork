# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
import datetime
import json
from decimal import Decimal

import graphene
import pytest

from ....checkout.models import CheckoutLine
from ...tests import DEALER_HEADERS
from ..models import DealerCustomer, DealerGroup, TierPrice
from ..no_stacking import LINE_METADATA_KEY, PRICE_OVERRIDE_REASON
from ..pricing import MAX_BATCH

pytestmark = pytest.mark.django_db

PRICES_URL = "/wsm/dealer_pricing/api/storefront/prices"
LINE_URL = "/wsm/dealer_pricing/api/checkout/dealer-line"
REPRICE_URL = "/wsm/dealer_pricing/api/checkout/dealer-line/reprice"

# What the storefront server sends. The key is now checked (saleor/wsm/http.py);
# `X-Saleor-Domain` still is not, because one process serves one tenant.
HEADERS = DEALER_HEADERS


@pytest.fixture
def dealer_group(customer_user):
    group = DealerGroup.objects.create(code="dealer-1", name="Dealer 1")
    DealerCustomer.objects.create(user=customer_user, group=group)
    return group


@pytest.fixture
def tiers(variant, dealer_group):
    TierPrice.objects.bulk_create(
        [
            TierPrice(variant=variant, group=dealer_group, min_quantity=5, amount=Decimal("8.00")),
            TierPrice(variant=variant, group=dealer_group, min_quantity=10, amount=Decimal("7.00")),
        ]
    )
    return dealer_group


def post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json", **HEADERS)


def gid(type_name, pk):
    return graphene.Node.to_global_id(type_name, pk)


def test_prices_endpoint_returns_the_ladder(client, variant, customer_user, tiers, channel_USD):
    response = post(
        client,
        PRICES_URL,
        {
            "customerId": gid("User", customer_user.pk),
            "channel": channel_USD.slug,
            "variantIds": [gid("ProductVariant", variant.pk)],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "breaks": {
            gid("ProductVariant", variant.pk): [
                {"minQuantity": 5, "amount": "8.00"},
                {"minQuantity": 10, "amount": "7.00"},
            ]
        }
    }


def test_prices_endpoint_refuses_more_than_the_cap(client, customer_user, channel_USD):
    response = post(
        client,
        PRICES_URL,
        {
            "customerId": gid("User", customer_user.pk),
            "channel": channel_USD.slug,
            "variantIds": [gid("ProductVariant", 1)] * (MAX_BATCH + 1),
        },
    )

    assert response.status_code == 413
    assert response.json() == {"error": "tooManyVariants"}


def test_prices_endpoint_is_empty_for_a_shopper_who_is_not_a_dealer(
    client, variant, staff_user, tiers, channel_USD
):
    response = post(
        client,
        PRICES_URL,
        {
            "customerId": gid("User", staff_user.pk),
            "channel": channel_USD.slug,
            "variantIds": [gid("ProductVariant", variant.pk)],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"breaks": {}}


def line_body(checkout, customer_user, variant, channel, quantity):
    return {
        "checkoutId": gid("Checkout", checkout.pk),
        "customerId": gid("User", customer_user.pk),
        "variantId": gid("ProductVariant", variant.pk),
        "quantity": quantity,
        "channel": channel.slug,
    }


def test_dealer_line_sets_price_override_and_metadata(
    client, checkout, variant, customer_user, tiers, channel_USD, stock
):
    response = post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 6))

    assert response.status_code == 200
    body = response.json()
    assert body["unitPrice"] == "8.00"
    assert body["dealerPrice"] == "8.00"
    assert body["minQuantity"] == 5

    line = CheckoutLine.objects.get(checkout_id=checkout.pk)
    assert line.price_override == Decimal("8.00")
    assert line.price_override_reason == PRICE_OVERRIDE_REASON
    assert json.loads(line.metadata[LINE_METADATA_KEY]) == {
        "group": "dealer-1",
        "minQuantity": 5,
    }
    assert body["lineId"] == gid("CheckoutLine", line.pk)


def test_a_half_cent_tier_is_charged_up_and_quoted_the_same(
    client, checkout, variant, customer_user, dealer_group, channel_USD
):
    """What is quoted and what is written are the same number, rounded up."""
    TierPrice.objects.create(
        variant=variant, group=dealer_group, min_quantity=1, amount=Decimal("8.005")
    )

    response = post(
        client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 1)
    )

    assert response.status_code == 200
    assert response.json()["dealerPrice"] == "8.01"
    line = CheckoutLine.objects.get(checkout_id=checkout.pk)
    assert line.price_override == Decimal("8.01")


def test_dealer_line_under_the_lowest_break_is_refused(
    client, checkout, variant, customer_user, tiers, channel_USD
):
    response = post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 4))

    assert response.status_code == 422
    assert response.json() == {"error": "belowBreak"}
    assert not CheckoutLine.objects.filter(checkout_id=checkout.pk).exists()


def test_dealer_line_in_another_channel_is_refused(
    client, checkout, variant, customer_user, tiers, channel_PLN
):
    response = post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_PLN, 6))

    assert response.status_code == 409
    assert response.json() == {"error": "channelMismatch"}


def test_a_shopper_who_is_not_a_dealer_gets_a_retail_line(
    client, checkout, variant, staff_user, tiers, channel_USD, stock
):
    response = post(client, LINE_URL, line_body(checkout, variant=variant, customer_user=staff_user, channel=channel_USD, quantity=6))

    assert response.status_code == 200
    assert response.json()["dealerPrice"] is None
    line = CheckoutLine.objects.get(checkout_id=checkout.pk)
    assert line.price_override is None
    assert LINE_METADATA_KEY not in line.metadata


def test_reprice_clears_the_override_when_the_quantity_falls_to_retail(
    client, checkout, variant, customer_user, tiers, channel_USD, stock
):
    post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 6))
    line = CheckoutLine.objects.get(checkout_id=checkout.pk)
    line.quantity = 2
    line.save(update_fields=["quantity"])

    body = {
        "checkoutId": gid("Checkout", checkout.pk),
        "customerId": gid("User", customer_user.pk),
        "lineId": gid("CheckoutLine", line.pk),
        "channel": channel_USD.slug,
    }
    first = post(client, REPRICE_URL, body)

    assert first.status_code == 200
    assert first.json() == {
        "lineId": gid("CheckoutLine", line.pk),
        "unitPrice": "10.00",
        "basePrice": "10.00",
        "dealerPrice": None,
    }
    line.refresh_from_db()
    assert line.price_override is None
    assert line.price_override_reason is None
    assert LINE_METADATA_KEY not in line.metadata

    # Idempotent: the answer is a function of the line and the catalog.
    assert post(client, REPRICE_URL, body).json() == first.json()


def test_reprice_puts_the_override_back_when_the_quantity_reaches_a_break(
    client, checkout, variant, customer_user, tiers, channel_USD, stock
):
    post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 6))
    line = CheckoutLine.objects.get(checkout_id=checkout.pk)
    line.quantity = 10
    line.save(update_fields=["quantity"])

    body = {
        "checkoutId": gid("Checkout", checkout.pk),
        "customerId": gid("User", customer_user.pk),
        "lineId": gid("CheckoutLine", line.pk),
        "channel": channel_USD.slug,
    }
    response = post(client, REPRICE_URL, body)

    assert response.json()["dealerPrice"] == "7.00"
    line.refresh_from_db()
    assert line.price_override == Decimal("7.00")
    assert json.loads(line.metadata[LINE_METADATA_KEY])["minQuantity"] == 10


def test_a_dealer_add_expires_the_checkout_prices(
    client, checkout, variant, customer_user, tiers, channel_USD
):
    """Endpoint 4 owes the checkout the same tail a stock mutation runs."""
    from django.utils import timezone

    from ....checkout.models import Checkout

    later = timezone.now() + datetime.timedelta(hours=1)
    Checkout.objects.filter(pk=checkout.pk).update(price_expiration=later)

    post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 6))

    checkout.refresh_from_db()
    assert checkout.price_expiration < later


def test_a_reprice_that_moves_the_price_expires_it_and_one_that_does_not_leaves_it(
    client, checkout, variant, customer_user, tiers, channel_USD
):
    """The write is idempotent, so the invalidation is too."""
    from django.utils import timezone

    from ....checkout.models import Checkout

    post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 6))
    line = CheckoutLine.objects.get(checkout_id=checkout.pk)
    body = {
        "checkoutId": gid("Checkout", checkout.pk),
        "customerId": gid("User", customer_user.pk),
        "lineId": gid("CheckoutLine", line.pk),
        "channel": channel_USD.slug,
    }

    # Nothing moved: the line is already on its break at this quantity.
    later = timezone.now() + datetime.timedelta(hours=1)
    Checkout.objects.filter(pk=checkout.pk).update(price_expiration=later)
    assert post(client, REPRICE_URL, body).json()["unitPrice"] == "8.00"
    checkout.refresh_from_db()
    assert checkout.price_expiration == later

    # The quantity reached the next rung, so the price moved and the prices die.
    CheckoutLine.objects.filter(pk=line.pk).update(quantity=10)
    assert post(client, REPRICE_URL, body).json()["unitPrice"] == "7.00"
    checkout.refresh_from_db()
    assert checkout.price_expiration < later



# --- finding 3: reprice never clears an override another app wrote -----------


def test_reprice_refuses_a_line_priced_by_another_app(
    client, checkout, variant, customer_user, tiers, channel_USD, stock
):
    """A compose-priced line is not this app's to zero out."""
    post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 6))
    line = CheckoutLine.objects.get(checkout_id=checkout.pk)
    line.quantity = 2
    line.price_override = Decimal("99.00")
    line.price_override_reason = "wsm.compose"
    line.save(update_fields=["quantity", "price_override", "price_override_reason"])

    response = post(
        client,
        REPRICE_URL,
        {
            "checkoutId": gid("Checkout", checkout.pk),
            "customerId": gid("User", customer_user.pk),
            "lineId": gid("CheckoutLine", line.pk),
            "channel": channel_USD.slug,
        },
    )

    assert response.status_code == 409
    assert response.json() == {"error": "foreignPriceOverride"}
    line.refresh_from_db()
    assert line.price_override == Decimal("99.00")
    assert line.price_override_reason == "wsm.compose"


# --- finding 8: the stock checks checkoutLinesAdd runs -----------------------


def test_dealer_line_refuses_a_variant_not_available_for_purchase(
    client, checkout, variant, customer_user, tiers, channel_USD, stock
):
    variant.product.channel_listings.filter(channel=channel_USD).update(
        available_for_purchase_at=None
    )

    response = post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 6))

    assert response.status_code == 422
    assert response.json()["error"] == "lineRefused"
    assert not CheckoutLine.objects.filter(checkout_id=checkout.pk).exists()


def test_dealer_line_refuses_a_quantity_over_the_checkout_limit(
    client, checkout, variant, customer_user, tiers, channel_USD, site_settings, stock
):
    site_settings.limit_quantity_per_checkout = 5
    site_settings.save(update_fields=["limit_quantity_per_checkout"])

    response = post(client, LINE_URL, line_body(checkout, customer_user, variant, channel_USD, 6))

    assert response.status_code == 422
    assert response.json()["error"] == "lineRefused"
    assert not CheckoutLine.objects.filter(checkout_id=checkout.pk).exists()


def test_dealer_line_refuses_a_quantity_that_is_not_a_number(
    client, checkout, variant, customer_user, tiers, channel_USD
):
    body = line_body(checkout, customer_user, variant, channel_USD, "x")

    assert post(client, LINE_URL, body).status_code == 422


def test_dealer_line_refuses_more_than_the_stock_on_hand(
    client, checkout, variant, customer_user, tiers, channel_USD, stock
):
    body = line_body(
        checkout, customer_user, variant, channel_USD, stock.quantity + 1
    )

    response = post(client, LINE_URL, body)

    assert response.status_code == 422
    assert response.json()["error"] == "lineRefused"
    assert not CheckoutLine.objects.filter(checkout_id=checkout.pk).exists()
