# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Prop 65 and shipping restrictions: what a product says, and where it may go.

Three surfaces, one row: the PDP payload and the configured-line response carry
the disclosure, the product carries it as a public stamp for everything that
reads a product, and the destination rule is enforced once, at order creation,
through the plugin the settings file registers.

The fail-safe cases are the ones worth the most here. A restriction that cannot
be decided must not block, because the failure it would cause (a store that
takes no orders in every state) is far worse than the one it would prevent (a
part shipped where it is not legal, which is a return).
"""

import base64
import json
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from saleor.account.models import Address
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.plugins.manager import get_plugins_manager
from saleor.shipping.models import ShippingZone
from saleor.wsm.compose.models import (
    PROP65_METAFIELD,
    PROP65_TEXT_METAFIELD,
    RESTRICTED_STATES_METAFIELD,
    RESTRICTION_MESSAGE_METAFIELD,
    ProductCompliance,
)
from saleor.wsm.compose.restrictions import is_destination_serviced
from saleor.wsm.tests import COMPOSE_HEADERS

OPTION_SETS_URL = "/wsm/compose/api/storefront/products/saleor/{}/option-sets"
CONFIGURED_LINE_URL = "/wsm/compose/api/checkout/configured-line"

# The supplier's own wording, which is what 5.0 carried where it carried any.
SUPPLIER_WORDING = (
    "WARNING: This product can expose you to chemicals including lead, which "
    "is known to the State of California to cause cancer."
)


def gid(type_name, pk):
    return base64.b64encode(f"{type_name}:{pk}".encode()).decode()


@pytest.fixture
def carb_part(variant, channel_USD):
    """Build a part that is not CARB legal: fine everywhere but California."""
    # Made to order, like the parts this models: the stock fixture ships zero
    # on hand, and stock is not what any of these tests are about.
    variant.track_inventory = False
    variant.save(update_fields=["track_inventory"])
    listing = variant.channel_listings.get(channel=channel_USD)
    listing.price_amount = Decimal("249.00")
    listing.discounted_price_amount = Decimal("249.00")
    listing.save(update_fields=["price_amount", "discounted_price_amount"])
    return variant.product


@pytest.fixture
def ca_restricted(carb_part):
    return ProductCompliance.objects.create(
        product=carb_part,
        prop65=True,
        restricted_states="CA",
    )


def us_address(state):
    return Address.objects.create(
        first_name="Jane",
        last_name="Doe",
        street_address_1="2000 Main Street",
        city="Somewhere",
        postal_code="92614",
        country_area=state,
        country="US",
    )


# --- the matcher -----------------------------------------------------------


def test_a_carb_part_is_not_serviced_in_california(ca_restricted, carb_part):
    serviced, refusals = is_destination_serviced([carb_part.pk], us_address("CA"))

    assert serviced is False
    assert [r.product_id for r in refusals] == [carb_part.pk]
    assert refusals[0].message == f"{carb_part.name} cannot be shipped to CA."


def test_only_the_named_state_is_refused(ca_restricted, carb_part):
    """A restriction is one state, not a store-wide switch."""
    serviced, refusals = is_destination_serviced([carb_part.pk], us_address("WA"))

    assert serviced is True
    assert refusals == []


def test_an_unreadable_destination_is_serviced(ca_restricted, carb_part):
    """Fail SAFE means do less: an address we cannot read never blocks.

    Both shapes of unreadable are here. `countryArea` is a free string whenever
    a caller turns Saleor's field normalization off, so "California" spelled out
    is a destination we cannot prove is CA, and no address at all is what a
    checkout carries before the shopper has typed one.
    """
    for address in (None, us_address("California"), us_address("")):
        serviced, refusals = is_destination_serviced([carb_part.pk], address)

        assert serviced is True, address
        assert refusals == []


def test_a_row_that_names_nowhere_restricts_nothing(carb_part):
    """The likeliest merchant mistake must not be the one that closes a store."""
    ProductCompliance.objects.create(product=carb_part, prop65=True)

    serviced, refusals = is_destination_serviced([carb_part.pk], us_address("CA"))

    assert serviced is True
    assert refusals == []


def test_a_product_with_no_row_at_all_is_serviced(carb_part):
    serviced, refusals = is_destination_serviced([carb_part.pk], us_address("CA"))

    assert (serviced, refusals) == (True, [])


def test_include_shipping_zones_is_an_allow_list(carb_part, address):
    """Named zones mean ONLY those countries; `address` is the PL fixture."""
    row = ProductCompliance.objects.create(product=carb_part)
    zone = ShippingZone.objects.create(name="United States", countries=["US"])
    row.include_shipping_zones.add(zone)

    assert is_destination_serviced([carb_part.pk], us_address("WA"))[0] is True

    serviced, refusals = is_destination_serviced([carb_part.pk], address)

    assert serviced is False
    assert refusals[0].message == f"{carb_part.name} cannot be shipped to PL."


def test_the_merchants_own_sentence_is_what_the_shopper_reads(ca_restricted, carb_part):
    ca_restricted.restriction_message = "Not CARB certified: no California sales."
    ca_restricted.save()

    _serviced, refusals = is_destination_serviced([carb_part.pk], us_address("CA"))

    assert refusals[0].message == "Not CARB certified: no California sales."


def test_a_typo_for_a_state_is_refused_on_the_screen(carb_part):
    """Caught where the mistake was made, not by an order that got through."""
    row = ProductCompliance(product=carb_part, restricted_states="CAL, CA")

    with pytest.raises(ValidationError) as refused:
        row.clean()

    assert "CAL" in str(refused.value)


def test_the_codes_are_normalised_on_the_way_in(carb_part):
    row = ProductCompliance(product=carb_part, restricted_states=" ca ,hi")
    row.clean()

    assert row.restricted_states == "CA, HI"
    assert row.state_codes == ("CA", "HI")


# --- the stamp -------------------------------------------------------------


def test_the_prop65_flag_lands_on_the_product(ca_restricted, carb_part):
    """The flag alone is the contract: no text means the standard warning."""
    carb_part.refresh_from_db()

    assert carb_part.metadata[PROP65_METAFIELD] == "true"
    assert PROP65_TEXT_METAFIELD not in carb_part.metadata


def test_the_merchants_own_wording_is_stamped_and_then_cleared(carb_part):
    row = ProductCompliance.objects.create(
        product=carb_part, prop65=True, prop65_text=SUPPLIER_WORDING
    )
    carb_part.refresh_from_db()
    assert carb_part.metadata[PROP65_TEXT_METAFIELD] == SUPPLIER_WORDING

    row.prop65 = False
    row.save()
    carb_part.refresh_from_db()

    # Absent, never blank: a reader takes a missing key as "no warning".
    assert PROP65_METAFIELD not in carb_part.metadata
    assert PROP65_TEXT_METAFIELD not in carb_part.metadata


def test_deleting_the_row_takes_the_stamp_with_it(ca_restricted, carb_part):
    ca_restricted.delete()
    carb_part.refresh_from_db()

    assert PROP65_METAFIELD not in carb_part.metadata
    assert RESTRICTED_STATES_METAFIELD not in carb_part.metadata
    assert RESTRICTION_MESSAGE_METAFIELD not in carb_part.metadata


def test_the_destination_rule_lands_on_the_product(ca_restricted, carb_part):
    """The checkout reads the rule off the product, not off a query.

    That is what lets it refuse an address before a gateway charges the
    card. The plugin still refuses at order creation; the stamp is what
    makes reaching that refusal abnormal.
    """
    carb_part.refresh_from_db()

    assert carb_part.metadata[RESTRICTED_STATES_METAFIELD] == "CA"
    # Present and blank: the states key is the gate, and blank means the
    # standard sentence, the one `refusal_message` builds.
    assert carb_part.metadata[RESTRICTION_MESSAGE_METAFIELD] == ""


def test_the_merchants_refusal_sentence_is_stamped(ca_restricted, carb_part):
    ca_restricted.restriction_message = "Not CARB certified: no California sales."
    ca_restricted.save()
    carb_part.refresh_from_db()

    assert (
        carb_part.metadata[RESTRICTION_MESSAGE_METAFIELD]
        == "Not CARB certified: no California sales."
    )


def test_several_states_are_stamped_the_way_the_row_stores_them(carb_part):
    row = ProductCompliance(product=carb_part, restricted_states=" ca ,hi")
    row.clean()
    row.save()
    carb_part.refresh_from_db()

    assert carb_part.metadata[RESTRICTED_STATES_METAFIELD] == "CA, HI"


def test_clearing_the_states_takes_both_keys_with_it(ca_restricted, carb_part):
    """A merchant who empties the field restricts nothing.

    The stamp has to say so: a stale key is a checkout refusing an address
    nobody banned.
    """
    ca_restricted.restriction_message = "No California sales."
    ca_restricted.save()
    carb_part.refresh_from_db()
    assert RESTRICTED_STATES_METAFIELD in carb_part.metadata

    ca_restricted.restricted_states = ""
    ca_restricted.save()
    carb_part.refresh_from_db()

    assert RESTRICTED_STATES_METAFIELD not in carb_part.metadata
    assert RESTRICTION_MESSAGE_METAFIELD not in carb_part.metadata
    # The disclosure is a separate decision and is untouched by this one.
    assert carb_part.metadata[PROP65_METAFIELD] == "true"


def test_a_disclosure_alone_stamps_no_restriction(carb_part):
    ProductCompliance.objects.create(product=carb_part, prop65=True)
    carb_part.refresh_from_db()

    assert RESTRICTED_STATES_METAFIELD not in carb_part.metadata
    assert RESTRICTION_MESSAGE_METAFIELD not in carb_part.metadata


# --- the two storefront surfaces -------------------------------------------


def test_the_option_sets_payload_carries_the_warning(client, carb_part):
    ProductCompliance.objects.create(
        product=carb_part, prop65=True, prop65_text=SUPPLIER_WORDING
    )

    body = client.get(OPTION_SETS_URL.format(gid("Product", carb_part.pk))).json()

    assert body["warnings"] == [{"type": "prop65", "text": SUPPLIER_WORDING}]


def test_the_payload_says_nothing_when_the_product_has_no_row(client, carb_part):
    body = client.get(OPTION_SETS_URL.format(gid("Product", carb_part.pk))).json()

    assert body["warnings"] == []


def test_a_restriction_alone_is_not_a_warning(client, carb_part):
    """A part that cannot go to California does not thereby need a Prop 65 badge."""
    ProductCompliance.objects.create(product=carb_part, restricted_states="CA")

    body = client.get(OPTION_SETS_URL.format(gid("Product", carb_part.pk))).json()

    assert body["warnings"] == []


def test_the_configured_line_response_carries_the_warning(
    client, checkout, carb_part, variant
):
    ProductCompliance.objects.create(product=carb_part, prop65=True)

    response = client.post(
        CONFIGURED_LINE_URL,
        data=json.dumps(
            {
                "checkoutId": gid("Checkout", checkout.token),
                "channel": checkout.channel.slug,
                "productId": gid("Product", carb_part.pk),
                "variantId": gid("ProductVariant", variant.pk),
                "quantity": 1,
                "selections": [],
                "acceptedFeeIds": [],
            }
        ),
        content_type="application/json",
        **COMPOSE_HEADERS,
    )

    assert response.status_code == 200, response.content
    assert response.json()["warnings"] == [{"type": "prop65"}]


# --- the seam --------------------------------------------------------------


PLUGIN_PATH = "saleor.wsm.compose.plugin.ComposeCompliancePlugin"


@pytest.fixture
def compliance_plugin(settings):
    """Name the plugin under test, the way every plugin test in this repo does.

    `saleor/tests/settings.py` sets `PLUGINS = []`, so a test that wants a
    plugin has to say so. The line that SHIPS is asserted separately, in
    `test_the_plugin_is_registered_in_the_settings_that_ship`, because a test
    that names the plugin itself can never prove the shipped settings do.
    """
    settings.PLUGINS = [PLUGIN_PATH]
    return PLUGIN_PATH


def test_the_plugin_is_registered_in_the_settings_that_ship(settings):
    """The seam is only a seam if the deployment loads it."""
    assert PLUGIN_PATH in settings.BUILTIN_PLUGINS


def _preprocess(checkout):
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, manager)
    return manager.preprocess_order_creation(checkout_info, lines)


def test_the_registered_plugin_refuses_a_california_order(
    checkout_with_item, compliance_plugin
):
    """End to end through the manager, not through the plugin class."""
    checkout = checkout_with_item
    product = checkout.lines.first().variant.product
    ProductCompliance.objects.create(product=product, restricted_states="CA")
    checkout.shipping_address = us_address("CA")
    checkout.save(update_fields=["shipping_address"])

    with pytest.raises(ValidationError) as refused:
        _preprocess(checkout)

    assert "cannot be shipped to CA" in str(refused.value)


def test_the_same_order_completes_to_any_other_state(
    checkout_with_item, compliance_plugin
):
    checkout = checkout_with_item
    product = checkout.lines.first().variant.product
    ProductCompliance.objects.create(product=product, restricted_states="CA")
    checkout.shipping_address = us_address("WA")
    checkout.save(update_fields=["shipping_address"])

    assert _preprocess(checkout) is None


def test_an_order_with_no_address_yet_is_not_refused(
    checkout_with_item, compliance_plugin
):
    """Fail safe at the seam, not only in the matcher."""
    checkout = checkout_with_item
    product = checkout.lines.first().variant.product
    ProductCompliance.objects.create(product=product, restricted_states="CA")
    checkout.shipping_address = None
    checkout.billing_address = None
    checkout.save(update_fields=["shipping_address", "billing_address"])

    assert _preprocess(checkout) is None
