# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The gated catalogue through the real GraphQL view, as a shopper meets it.

Every assertion goes through `reverse("api")`, which under this branch is the
composed schema `saleor/wsm/urls.py` mounts, because the thing being proved is
what the SERVER answers a request: a resolver called by hand could not tell the
difference between a field that exists and a field that is served, and could not
prove that the price surfaces answer null.
"""

from decimal import Decimal

import graphene
import pytest

from ....graphql.tests.utils import assert_no_permission, get_graphql_content
from ...dealer.models import (
    DealerCustomer,
    DealerGroup,
    DealerProductGate,
    DealerSettings,
    TierPrice,
)

pytestmark = pytest.mark.django_db

PDP = """
    query Pdp($id: ID!, $channel: String!) {
      product(id: $id, channel: $channel) {
        id
        wsmGated
        pricing { priceRange { start { gross { amount } } } }
      }
    }
"""

VARIANT_PDP = """
    query VariantPdp($id: ID!, $channel: String!) {
      productVariant(id: $id, channel: $channel) {
        id
        pricing { price { gross { amount } } }
      }
    }
"""

GATE_READ = """
    query Gate($id: ID!, $channel: String!) {
      product(id: $id, channel: $channel) {
        wsmGate { loginRequired groups { code } }
      }
    }
"""

GATE_SET = """
    mutation Set($product: ID!, $required: Boolean!, $groups: [ID!]) {
      wsmProductGateSet(
        product: $product, loginRequired: $required, groups: $groups
      ) {
        gate { loginRequired groups { code } }
        errors { field code message }
      }
    }
"""

GATE_BULK = """
    mutation Bulk($gates: [WsmProductGateSetInput!]!) {
      wsmProductGateBulkSet(gates: $gates) {
        count
        errors { field code message }
      }
    }
"""

SETTINGS_UPDATE = """
    mutation Settings($gated: Boolean!) {
      wsmDealerSettingsUpdate(
        input: {discountStacking: false, catalogueGated: $gated}
      ) {
        dealerSettings { catalogueGated }
        errors { field code message }
      }
    }
"""

CHECKOUT_CREATE = """
    mutation Buy($channel: String!, $variant: ID!) {
      checkoutCreate(
        input: {
          channel: $channel
          email: "buyer@example.com"
          lines: [{quantity: 1, variantId: $variant}]
        }
      ) {
        checkout { id }
        errors { field code message }
      }
    }
"""


@pytest.fixture
def fob():
    return DealerGroup.objects.create(code="fob", name="FOB")


@pytest.fixture
def cif():
    return DealerGroup.objects.create(code="cif", name="CIF")


@pytest.fixture
def fob_buyer(customer_user, fob):
    DealerCustomer.objects.create(user=customer_user, group=fob)
    return customer_user


@pytest.fixture
def gated_store():
    return DealerSettings.objects.create(catalogue_gated=True)


def product_id(product):
    return graphene.Node.to_global_id("Product", product.pk)


def ask(client, query, product, channel_USD, **extra):
    variables = {"id": product_id(product), "channel": channel_USD.slug, **extra}
    return get_graphql_content(client.post_graphql(query, variables))


def test_an_anonymous_shopper_sees_no_price_on_a_gated_store(
    api_client, product, channel_USD, gated_store
):
    """The requirement, in one assertion: the public may browse and sees no
    price. Null is stock's own shape for "there is no pricing here", so no
    storefront needs a new one."""
    data = ask(api_client, PDP, product, channel_USD)["data"]["product"]

    assert data["wsmGated"] is True
    assert data["pricing"] is None


def test_an_anonymous_shopper_sees_the_price_on_an_ungated_store(
    api_client, product, channel_USD
):
    """The other half, and the one that says this feature is off by default."""
    data = ask(api_client, PDP, product, channel_USD)["data"]["product"]

    assert data["wsmGated"] is False
    assert data["pricing"]["priceRange"]["start"]["gross"]["amount"] > 0


def test_a_dealer_sees_the_price_on_a_gated_store(
    user_api_client, product, channel_USD, gated_store, fob_buyer
):
    data = ask(user_api_client, PDP, product, channel_USD)["data"]["product"]

    assert data["wsmGated"] is False
    assert data["pricing"] is not None


def test_a_signed_in_shopper_who_is_not_a_dealer_still_sees_nothing(
    user_api_client, product, channel_USD, gated_store
):
    """An account is not a dealer account. `DealerCustomer` is what is."""
    data = ask(user_api_client, PDP, product, channel_USD)["data"]["product"]

    assert data["wsmGated"] is True
    assert data["pricing"] is None


def test_a_product_scoped_to_one_group_is_gated_for_the_other(
    user_api_client, product, channel_USD, gated_store, fob, cif, customer_user
):
    """Trade tiers are VISIBILITY: the CIF dealer may not see the FOB part."""
    DealerCustomer.objects.create(user=customer_user, group=cif)
    gate = DealerProductGate.objects.create(product=product, login_required=True)
    gate.groups.add(fob)

    data = ask(user_api_client, PDP, product, channel_USD)["data"]["product"]

    assert data["wsmGated"] is True
    assert data["pricing"] is None


def test_the_one_public_product_on_a_gated_store_is_still_priced(
    api_client, product, channel_USD, gated_store
):
    """ds gates 2,731 of 2,732 products and publishes one."""
    DealerProductGate.objects.create(product=product, login_required=False)

    data = ask(api_client, PDP, product, channel_USD)["data"]["product"]

    assert data["wsmGated"] is False
    assert data["pricing"] is not None


def test_the_variant_price_is_null_too(
    api_client, variant, channel_USD, gated_store
):
    """`ProductVariant.pricing` is the other public surface that hands a
    shopper a number, so hiding one and not the other would hide nothing."""
    variables = {
        "id": graphene.Node.to_global_id("ProductVariant", variant.pk),
        "channel": channel_USD.slug,
    }
    content = get_graphql_content(api_client.post_graphql(VARIANT_PDP, variables))

    assert content["data"]["productVariant"]["pricing"] is None


def test_a_dealer_still_gets_their_tier_price_on_a_gated_store(
    variant, fob_buyer, channel_USD, gated_store
):
    """The gate hides the public price; it never touches the dealer's."""
    from ...dealer.pricing import dealer_price_for

    TierPrice.objects.create(
        variant=variant,
        group=DealerGroup.objects.get(code="fob"),
        min_quantity=1,
        amount=Decimal("5.00"),
    )

    assert dealer_price_for(
        variant, fob_buyer, 1, channel=channel_USD
    ).amount == Decimal("5.00")


def test_an_anonymous_add_to_cart_is_refused(
    api_client, variant, channel_USD, gated_store
):
    variables = {
        "channel": channel_USD.slug,
        "variant": graphene.Node.to_global_id("ProductVariant", variant.pk),
    }
    content = get_graphql_content(api_client.post_graphql(CHECKOUT_CREATE, variables))
    payload = content["data"]["checkoutCreate"]

    assert payload["checkout"] is None
    assert payload["errors"], "a gated variant must not reach a cart"
    assert "Sign in with a dealer account" in payload["errors"][0]["message"]
    assert variant.sku in payload["errors"][0]["message"]


def test_a_dealer_add_to_cart_succeeds(
    user_api_client, variant, channel_USD, gated_store, fob_buyer
):
    variables = {
        "channel": channel_USD.slug,
        "variant": graphene.Node.to_global_id("ProductVariant", variant.pk),
    }
    content = get_graphql_content(
        user_api_client.post_graphql(CHECKOUT_CREATE, variables)
    )
    payload = content["data"]["checkoutCreate"]

    assert payload["errors"] == []
    assert payload["checkout"] is not None


def test_the_merchant_gate_field_needs_the_permission(
    staff_api_client, product, channel_USD
):
    response = staff_api_client.post_graphql(
        GATE_READ, {"id": product_id(product), "channel": channel_USD.slug}
    )

    assert_no_permission(response)


def test_the_gate_mutation_writes_and_reads_back(
    staff_api_client, permission_manage_discounts, product, channel_USD, fob
):
    variables = {
        "product": product_id(product),
        "required": True,
        "groups": [graphene.Node.to_global_id("WsmDealerGroup", fob.pk)],
    }
    content = get_graphql_content(
        staff_api_client.post_graphql(
            GATE_SET, variables, permissions=[permission_manage_discounts]
        )
    )
    payload = content["data"]["wsmProductGateSet"]

    assert payload["errors"] == []
    assert payload["gate"]["loginRequired"] is True
    assert [group["code"] for group in payload["gate"]["groups"]] == ["fob"]

    content = get_graphql_content(
        staff_api_client.post_graphql(
            GATE_READ, {"id": product_id(product), "channel": channel_USD.slug}
        )
    )
    assert content["data"]["product"]["wsmGate"]["loginRequired"] is True


def test_the_gate_mutation_rewrites_rather_than_duplicating(
    staff_api_client, permission_manage_discounts, product, fob
):
    """The Dashboard toggle round-trips: true, false, true, one row throughout."""
    for required in (True, False, True):
        content = get_graphql_content(
            staff_api_client.post_graphql(
                GATE_SET,
                {"product": product_id(product), "required": required, "groups": []},
                permissions=[permission_manage_discounts],
            )
        )
        assert content["data"]["wsmProductGateSet"]["errors"] == []

    assert DealerProductGate.objects.count() == 1
    assert DealerProductGate.objects.get().login_required is True


def test_the_gate_mutation_needs_the_permission(staff_api_client, product):
    response = staff_api_client.post_graphql(
        GATE_SET, {"product": product_id(product), "required": True, "groups": []}
    )

    assert_no_permission(response)


def test_the_gate_mutation_refuses_an_unknown_product(
    staff_api_client, permission_manage_discounts
):
    content = get_graphql_content(
        staff_api_client.post_graphql(
            GATE_SET,
            {
                "product": graphene.Node.to_global_id("Product", 987654),
                "required": True,
                "groups": [],
            },
            permissions=[permission_manage_discounts],
        )
    )
    errors = content["data"]["wsmProductGateSet"]["errors"]

    assert errors and errors[0]["code"] == "NOT_FOUND"


def test_the_bulk_mutation_gates_a_catalogue(
    staff_api_client, permission_manage_discounts, product_list
):
    """ds needs 2,731 products gated, which is six calls of 500, not a job."""
    gates = [
        {"product": product_id(product), "loginRequired": True}
        for product in product_list
    ]
    content = get_graphql_content(
        staff_api_client.post_graphql(
            GATE_BULK, {"gates": gates}, permissions=[permission_manage_discounts]
        )
    )
    payload = content["data"]["wsmProductGateBulkSet"]

    assert payload["errors"] == []
    assert payload["count"] == len(product_list)
    assert DealerProductGate.objects.filter(login_required=True).count() == len(
        product_list
    )


def test_the_bulk_mutation_is_an_upsert(
    staff_api_client, permission_manage_discounts, product_list
):
    gates = [
        {"product": product_id(product), "loginRequired": True}
        for product in product_list
    ]
    for _ in range(2):
        get_graphql_content(
            staff_api_client.post_graphql(
                GATE_BULK, {"gates": gates}, permissions=[permission_manage_discounts]
            )
        )

    assert DealerProductGate.objects.count() == len(product_list)


def test_the_bulk_mutation_refuses_a_paste_over_the_cap(
    staff_api_client, permission_manage_discounts, product
):
    from ..utils import BULK_LIMIT

    gates = [{"product": product_id(product), "loginRequired": True}] * (
        BULK_LIMIT + 1
    )
    content = get_graphql_content(
        staff_api_client.post_graphql(
            GATE_BULK, {"gates": gates}, permissions=[permission_manage_discounts]
        )
    )
    payload = content["data"]["wsmProductGateBulkSet"]

    assert payload["count"] == 0
    assert payload["errors"][0]["code"] == "BULK_LIMIT"


def test_the_bulk_mutation_refuses_the_same_product_twice(
    staff_api_client, permission_manage_discounts, product
):
    gates = [
        {"product": product_id(product), "loginRequired": True},
        {"product": product_id(product), "loginRequired": False},
    ]
    content = get_graphql_content(
        staff_api_client.post_graphql(
            GATE_BULK, {"gates": gates}, permissions=[permission_manage_discounts]
        )
    )
    payload = content["data"]["wsmProductGateBulkSet"]

    assert payload["count"] == 0
    assert payload["errors"][0]["code"] == "UNIQUE"


def test_the_settings_switch_gates_the_whole_catalogue(
    staff_api_client, permission_manage_discounts, api_client, product, channel_USD
):
    """One switch, and every product answers differently on the next request."""
    before = ask(api_client, PDP, product, channel_USD)["data"]["product"]
    assert before["pricing"] is not None

    content = get_graphql_content(
        staff_api_client.post_graphql(
            SETTINGS_UPDATE, {"gated": True}, permissions=[permission_manage_discounts]
        )
    )
    assert content["data"]["wsmDealerSettingsUpdate"]["errors"] == []
    assert (
        content["data"]["wsmDealerSettingsUpdate"]["dealerSettings"]["catalogueGated"]
        is True
    )

    after = ask(api_client, PDP, product, channel_USD)["data"]["product"]
    assert after["wsmGated"] is True
    assert after["pricing"] is None


def test_a_staff_token_that_manages_products_still_sees_prices(
    staff_api_client, permission_manage_products, product, channel_USD, gated_store
):
    """A merchant reading their own catalogue is not a gated shopper."""
    staff_api_client.user.user_permissions.add(permission_manage_products)

    content = get_graphql_content(
        staff_api_client.post_graphql(
            PDP, {"id": product_id(product), "channel": channel_USD.slug}
        )
    )

    assert content["data"]["product"]["pricing"] is not None
    assert content["data"]["product"]["wsmGated"] is False
