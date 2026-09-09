# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Can a merchant FIND the row and read the screen?

Every assertion here is a finding from the merchant walk of 2026-09-08, which
rated these screens BLOCKED: 626 tier prices all reading "Base" with no search
box, dealer customers listed as UUIDs, foreign keys with no lookup behind them,
and a dealer settings list that was empty and said nothing about the default.

The client is a STAFF NON-SUPERUSER holding only the model permissions, because
a superuser passes every check these screens make and would prove nothing.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from ....permission.models import Permission
from ...compose.admin import site as merchant_site
from ..models import DealerCustomer, DealerGroup, DealerSettings, TierPrice

pytestmark = pytest.mark.django_db

AUTH_BACKEND = "saleor.wsm.compose.auth.AdminPasswordBackend"
TIER_PRICES = "/admin/wsm_dealer/tierprice/"
DEALER_GROUPS = "/admin/wsm_dealer/dealergroup/"
DEALER_CUSTOMERS = "/admin/wsm_dealer/dealercustomer/"
DEALER_SETTINGS = "/admin/wsm_dealer/dealersettings/"
AUTOCOMPLETE = "/admin/autocomplete/"


def grant(user, *dotted_permissions):
    """Give the user exactly these permissions, loudly if one does not exist."""
    for dotted in dotted_permissions:
        app_label, codename = dotted.split(".")
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        )


@pytest.fixture(autouse=True)
def deployed_middleware(settings):
    """Run the admin the way the box runs it.

    `saleor/tests/settings.py` inserts `restrict_writer_middleware` AHEAD of the
    session middleware, so under the test settings every admin request 500s on
    its own session read: the Django admin reads the session, the user and its
    permissions from the default connection by design, before any view runs.
    The deployed setting is `ENABLE_RESTRICT_WRITER_MIDDLEWARE` off, which is
    what these tests therefore run under. Making /admin/ survive that middleware
    would take an `allow_writer` around the whole mount in wsm.compose's
    AdminSite; it is a finding on that file, not something these screens can fix.
    """
    settings.MIDDLEWARE = [
        middleware
        for middleware in settings.MIDDLEWARE
        if "restrict_writer" not in middleware
    ]

@pytest.fixture
def merchant(client, staff_user):
    assert staff_user.is_staff and not staff_user.is_superuser
    grant(
        staff_user,
        "wsm_dealer.view_dealergroup",
        "wsm_dealer.view_dealercustomer",
        "wsm_dealer.add_dealercustomer",
        "wsm_dealer.change_dealercustomer",
        "wsm_dealer.view_tierprice",
        "wsm_dealer.add_tierprice",
        "wsm_dealer.change_tierprice",
        "wsm_dealer.view_dealersettings",
        "wsm_dealer.add_dealersettings",
        "wsm_dealer.change_dealersettings",
    )
    client.force_login(staff_user, backend=AUTH_BACKEND)
    return client


@pytest.fixture
def group(db):
    return DealerGroup.objects.create(code="dealer-1", name="Dealer 1")


@pytest.fixture
def two_skus(variant, product_type, category):
    """Two SKUs on two products, named so that one search term reaches one row.

    `product_list` will not do: its SKUs are random hex, so a search for
    "Test product 1" matches a decoy whose SKU happens to contain a 1.
    """
    from ....product.models import Product, ProductVariant

    other = Product.objects.create(
        name="Bushwacker Pocket Flare",
        slug="bushwacker-pocket-flare",
        product_type=product_type,
        category=category,
    )
    return variant, ProductVariant.objects.create(product=other, sku="BW-40919-02")


def price(variant, group, amount="9.000"):
    return TierPrice.objects.create(
        variant=variant, group=group, min_quantity=1, amount=Decimal(amount)
    )


# --- the tier price list ------------------------------------------------------


def test_tier_price_list_names_the_product_and_the_sku(merchant, variant, group):
    """"Base" is the variant name on every single-variant product in the fleet."""
    price(variant, group)

    response = merchant.get(TIER_PRICES)

    assert response.status_code == 200
    body = response.content.decode()
    assert variant.product.name in body
    assert variant.sku in body


def test_tier_price_search_by_sku_returns_only_that_row(merchant, two_skus, group):
    decoy, wanted = two_skus
    price(wanted, group)
    price(decoy, group)

    body = merchant.get(TIER_PRICES, {"q": wanted.sku}).content.decode()

    assert wanted.sku in body
    assert decoy.sku not in body


def test_tier_price_search_by_product_name_returns_only_that_row(
    merchant, two_skus, group
):
    decoy, wanted = two_skus
    price(wanted, group)
    price(decoy, group)

    body = merchant.get(TIER_PRICES, {"q": "Bushwacker"}).content.decode()

    assert wanted.sku in body
    assert decoy.sku not in body


def test_tier_price_list_shows_two_places_and_the_currency(merchant, variant, group):
    """"228.000" reads as a bug and "0.000" reads as free. Storage keeps 3 places."""
    price(variant, group, amount="228.000")

    body = merchant.get(TIER_PRICES).content.decode()

    assert "228.00 USD" in body
    assert "228.000" not in body


def test_tier_price_form_takes_two_places_and_names_the_currency(
    merchant, variant, group
):
    """The input a merchant types into, not just the column they read."""
    row = price(variant, group, amount="228.000")

    body = merchant.get(f"{TIER_PRICES}{row.pk}/change/").content.decode()

    assert "What this group pays each (USD)" in body
    assert 'value="228.00"' in body


def test_tier_price_form_refuses_a_tenth_of_a_cent(merchant, variant, group):
    """Three places is a storage decision, never something a merchant means."""
    response = merchant.post(
        f"{TIER_PRICES}add/",
        {
            "variant": variant.pk,
            "group": group.pk,
            "min_quantity": "1",
            "amount": "228.004",
        },
    )

    assert response.status_code == 200
    assert not TierPrice.objects.filter(variant=variant).exists()


# --- the dealer customer list -------------------------------------------------


def test_dealer_customer_list_names_the_shopper(merchant, customer_user, group):
    """Saleor's `User.__str__` is a UUID, which names nobody."""
    DealerCustomer.objects.create(user=customer_user, group=group)

    body = merchant.get(DEALER_CUSTOMERS).content.decode()

    assert customer_user.email in body
    assert str(customer_user.uuid) not in body


def test_dealer_customer_search_by_email(merchant, customer_user, customer_user2, group):
    """The decoy is another shopper: the signed-in staff email is in the header."""
    DealerCustomer.objects.create(user=customer_user, group=group)
    DealerCustomer.objects.create(user=customer_user2, group=group)

    body = merchant.get(DEALER_CUSTOMERS, {"q": customer_user.email}).content.decode()

    assert customer_user.email in body
    assert customer_user2.email not in body


# --- the pickers --------------------------------------------------------------


def test_the_user_picker_answers_the_autocomplete(merchant, customer_user):
    response = merchant.get(
        AUTOCOMPLETE,
        {
            "app_label": "wsm_dealer",
            "model_name": "dealercustomer",
            "field_name": "user",
            "term": customer_user.email,
        },
    )

    assert response.status_code == 200
    texts = [result["text"] for result in response.json()["results"]]
    assert any(customer_user.email in text for text in texts), texts


def test_the_variant_picker_puts_the_exact_sku_first(merchant, variant):
    """Same defect, same fix, on the picker that finds a SKU to price.

    The decoys sort ahead of the exact match on SKU, which is the only order
    this picker had, so a merchant pricing a tier for 71801 was offered two
    Truxedo covers before the part they typed.
    """
    from ....product.models import ProductVariant

    for sku in ("1471801", "1571801", "71801"):
        ProductVariant.objects.create(product=variant.product, sku=sku, name="Base")

    response = merchant.get(
        AUTOCOMPLETE,
        {
            "app_label": "wsm_dealer",
            "model_name": "tierprice",
            "field_name": "variant",
            "term": "71801",
        },
    )

    texts = [result["text"] for result in response.json()["results"]]
    assert texts[0].endswith("[71801]"), texts
    assert len(texts) == 3, texts


def test_the_variant_picker_names_the_product_and_the_sku(merchant, variant):
    response = merchant.get(
        AUTOCOMPLETE,
        {
            "app_label": "wsm_dealer",
            "model_name": "tierprice",
            "field_name": "variant",
            "term": variant.sku,
        },
    )

    assert response.status_code == 200
    texts = [result["text"] for result in response.json()["results"]]
    assert len(texts) == 1, texts
    assert variant.product.name in texts[0]
    assert variant.sku in texts[0]


def test_the_pickers_stay_off_the_index_and_refuse_writes(merchant):
    index = merchant.get("/admin/").content.decode()

    assert "Tier prices" in index
    assert "/admin/account/user/" not in index
    assert merchant.get("/admin/account/user/add/").status_code == 403


# --- dealer settings ----------------------------------------------------------


def test_dealer_settings_opens_the_one_row_and_states_the_default(merchant):
    """An empty list told the walk nothing. The row says what stacking does."""
    assert not DealerSettings.objects.exists()

    response = merchant.get(DEALER_SETTINGS)

    assert response.status_code == 302
    row = DealerSettings.objects.get()
    assert row.discount_stacking is False
    assert response["Location"].endswith(f"{DEALER_SETTINGS}{row.pk}/change/")
    page = merchant.get(response["Location"]).content.decode()
    assert "takes no" in page and "voucher" in page


def test_dealer_settings_does_not_make_a_second_row(merchant):
    merchant.get(DEALER_SETTINGS)
    merchant.get(DEALER_SETTINGS)

    assert DealerSettings.objects.count() == 1


# --- the tier group namespace, for wsm.compose to call ------------------------


def test_dealer_group_codes_lists_the_namespace(group):
    DealerGroup.objects.create(code="warehouse")

    assert DealerGroup.objects.codes() == ["dealer-1", "warehouse"]


def test_validate_tier_group_code_refuses_a_code_with_no_group(group):
    from ..models import validate_tier_group_code

    with pytest.raises(ValidationError) as refusal:
        validate_tier_group_code("dealer-9")

    assert "dealer-1" in str(refusal.value)


def test_validate_tier_group_code_accepts_a_real_code(group):
    from ..models import validate_tier_group_code

    assert validate_tier_group_code("dealer-1") is None


def test_the_settings_singleton_offers_no_add_screen(merchant):
    """Add was the only doorway on a table that may hold exactly one row.

    A merchant granted the settings permissions saw ADD, filled the form in and
    got "already exists" from a unique constraint, with no way to reach the row
    that already existed. The changelist redirect below is the doorway; Add is
    now refused even for the user who holds `add_dealersettings`.
    """
    DealerSettings.objects.all().delete()

    refused_empty = merchant.get(f"{DEALER_SETTINGS}add/")
    landed = merchant.get(DEALER_SETTINGS)
    row = DealerSettings.objects.get()
    refused_full = merchant.get(f"{DEALER_SETTINGS}add/")

    # Refused with no row too: the doorway makes the row, so Add is never the
    # way in, and a merchant who lands on Add cannot reach the row that exists.
    assert refused_empty.status_code == 403
    assert landed["Location"].endswith(f"{DEALER_SETTINGS}{row.pk}/change/")
    assert refused_full.status_code == 403


# --- the dealer group form ----------------------------------------------------


def test_dealer_group_form_summarises_its_tier_prices(
    merchant, two_skus, group, channel_USD
):
    """The walk opened a group holding 304 prices and saw two text fields."""
    decoy, wanted = two_skus
    price(wanted, group, amount="12.000")
    price(decoy, group, amount="3498.990")

    body = merchant.get(f"{DEALER_GROUPS}{group.pk}/change/").content.decode()

    assert "2 tier prices" in body
    assert "lowest 12.00 USD" in body
    assert "highest 3498.99 USD" in body


def test_dealer_group_form_links_to_this_groups_tier_prices(
    merchant, two_skus, group, channel_USD
):
    """One door, pre-filtered, into the list that can already page 304 rows."""
    decoy, wanted = two_skus
    price(wanted, group)

    body = merchant.get(f"{DEALER_GROUPS}{group.pk}/change/").content.decode()

    assert f'href="{TIER_PRICES}?group__id__exact={group.pk}"' in body
    assert "See this group" in body


def test_dealer_group_form_says_so_when_the_group_prices_nothing(merchant, group):
    """An empty group is a real state: a code that nothing points at yet."""
    body = merchant.get(f"{DEALER_GROUPS}{group.pk}/change/").content.decode()

    assert "No tier prices yet" in body
    assert f'href="{TIER_PRICES}add/"' in body


def test_dealer_group_summary_costs_two_queries(
    two_skus, group, channel_USD, django_assert_num_queries
):
    """One aggregate and one currency, per PAGE. An inline would be 304 rows."""
    decoy, wanted = two_skus
    price(wanted, group)
    price(decoy, group)
    model_admin = merchant_site.get_model_admin(DealerGroup)

    with django_assert_num_queries(2):
        model_admin.tier_price_summary(group)


def test_dealer_group_list_carries_no_summary(merchant, two_skus, group, channel_USD):
    """The summary answers a question asked on one group, so the list pays nothing."""
    decoy, wanted = two_skus
    price(wanted, group)

    body = merchant.get(DEALER_GROUPS).content.decode()

    assert "tier prices, lowest" not in body
