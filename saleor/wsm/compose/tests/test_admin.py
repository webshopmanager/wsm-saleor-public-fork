# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The admin, rendered as a merchant, with the writer guard switched on.

`restrict_writer_middleware` is what production runs to stop an unrouted query
reaching the writer database, and Django's admin reaches for the writer on every
page: the session, the permission rows, the LogEntry it writes on save. Without
`allow_writer` around the mount, every merchant screen answers 500 the moment
that middleware is enabled, which is the state the box is one environment
variable away from.
"""

from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from saleor.permission.models import Permission
from saleor.wsm.compose import pricing
from django.db import connection
from django.test.utils import CaptureQueriesContext

from saleor.wsm.compose.models import (
    DealerTierOptionPrice,
    Fee,
    OptionSet,
    OptionValue,
)

BACKEND = "saleor.wsm.compose.auth.AdminPasswordBackend"


@pytest.fixture
def merchant(staff_user):
    """Staff, not superuser: a superuser short-circuits the permission reads."""
    staff_user.user_permissions.set(
        Permission.objects.filter(
            content_type__in=ContentType.objects.filter(app_label="wsm_compose")
        )
    )
    return staff_user


def test_the_writer_guard_is_on_in_this_run():
    """A test that proves nothing is not a pass. This one names the guard."""
    assert any("restrict_writer_middleware" in name for name in settings.MIDDLEWARE), (
        settings.MIDDLEWARE
    )


@pytest.mark.django_db
def test_the_admin_index_renders_for_a_merchant(client, merchant):
    client.force_login(merchant, backend=BACKEND)

    response = client.get("/admin/")

    assert response.status_code == 200
    assert b"Option sets" in response.content


@pytest.mark.django_db
def test_an_admin_changelist_renders_for_a_merchant(client, merchant):
    """The index alone would miss the ModelAdmin views, mounted a level down."""
    client.force_login(merchant, backend=BACKEND)

    response = client.get("/admin/wsm_compose/optionset/")

    assert response.status_code == 200


@pytest.mark.django_db
def test_the_login_page_renders(client):
    """`login` is mounted without `admin_view`, so it needs the wrap of its own."""
    response = client.get("/admin/login/")

    assert response.status_code == 200


@pytest.mark.django_db
def test_every_admin_response_carries_x_robots_tag(client, merchant):
    """The console shares a host with the public API, so login is crawlable.

    Asserted on the anonymous login page and on a logged-in screen, because the
    header is set at the site and not on one view: a merchant screen that lost
    it would mean the wrap had been moved, not that one template changed.
    """
    anonymous = client.get("/admin/login/")

    assert anonymous.status_code == 200
    assert anonymous["X-Robots-Tag"] == "noindex, nofollow"

    client.force_login(merchant, backend=BACKEND)
    index = client.get("/admin/")

    assert index.status_code == 200
    assert index["X-Robots-Tag"] == "noindex, nofollow"


@pytest.mark.django_db
def test_the_fee_list_shows_money_with_its_currency(client, merchant, product):
    """A charge with no currency on it is a number a merchant has to guess at."""
    client.force_login(merchant, backend=BACKEND)
    Fee.objects.create(
        product=product, label="Freight crating", basis=pricing.FIXED, amount=Decimal("149")
    )

    body = client.get("/admin/wsm_compose/fee/").content.decode()

    assert "149.00 USD" in body


@pytest.mark.django_db
def test_the_fee_list_shows_a_percentage_as_a_percentage(client, merchant, product):
    """The same column carries two units; the basis is what says which."""
    client.force_login(merchant, backend=BACKEND)
    Fee.objects.create(
        product=product, label="Handling", basis=pricing.PERCENT, amount=Decimal("8.25")
    )

    body = client.get("/admin/wsm_compose/fee/").content.decode()

    assert "8.25%" in body
    assert "8.25 USD" not in body


@pytest.mark.django_db
def test_the_option_value_list_signs_the_price_change(client, merchant, product):
    """A credit and a surcharge are the same string without the sign."""
    client.force_login(merchant, backend=BACKEND)
    option_set = OptionSet.objects.create(product=product, name="Color", label="Colour")
    OptionValue.objects.create(
        option_set=option_set, name="Black", price_delta=Decimal("25")
    )
    OptionValue.objects.create(
        option_set=option_set, name="Omit filter", price_delta=Decimal("-29.99")
    )

    body = client.get("/admin/wsm_compose/optionvalue/").content.decode()

    assert "+25.00 USD" in body
    assert "-29.99 USD" in body


@pytest.mark.django_db
def test_no_lookup_shows_a_bare_row_id(client, merchant):
    """`raw_id_fields` is a number box: the merchant sees 800001, not a product.

    Asserted over the registry rather than the three known offenders, because
    the next ModelAdmin registered here is the one that would bring it back.
    """
    from saleor.wsm.compose.admin import site as merchant_site

    offenders = {}
    for model, model_admin in merchant_site._registry.items():
        holders = [model_admin] + [inline(model, merchant_site) for inline in model_admin.inlines]
        for holder in holders:
            if getattr(holder, "raw_id_fields", ()):
                offenders[type(holder).__name__] = holder.raw_id_fields

    assert offenders == {}
    assert merchant_site._registry[OptionSet].autocomplete_fields == ("product",)


@pytest.mark.django_db
def test_a_question_links_to_the_product_it_is_asked_on(client, merchant, product):
    """There was no path from a product to what it asks the shopper."""
    client.force_login(merchant, backend=BACKEND)
    OptionSet.objects.create(product=product, name="Color", label="Colour")

    body = client.get("/admin/wsm_compose/optionset/").content.decode()

    assert f"/admin/product/product/?q={product.slug}" in body


@pytest.mark.django_db
def test_a_list_opens_for_one_product(client, merchant, product_list):
    """The link a product row points at has to answer, not 500 on the lookup."""
    client.force_login(merchant, backend=BACKEND)
    first, second = product_list[0], product_list[1]
    OptionSet.objects.create(product=first, name="Tank size", label="Tank size")
    OptionSet.objects.create(product=second, name="Pump", label="Pump wiring")
    Fee.objects.create(
        product=first, label="Crating", basis=pricing.FIXED, amount=Decimal("149")
    )

    sets = client.get(
        f"/admin/wsm_compose/optionset/?product__id__exact={first.pk}"
    )
    fees = client.get(f"/admin/wsm_compose/fee/?product__id__exact={second.pk}")

    assert sets.status_code == 200
    assert "Tank size" in sets.content.decode()
    assert "Pump wiring" not in sets.content.decode()
    assert fees.status_code == 200
    assert "Crating" not in fees.content.decode()


@pytest.mark.django_db
def test_a_product_row_counts_its_questions_and_charges(client, merchant, product):
    """The product screen is where a merchant starts, so it says what is on it."""
    client.force_login(merchant, backend=BACKEND)
    OptionSet.objects.create(product=product, name="Color", label="Colour")
    Fee.objects.create(
        product=product, label="Crating", basis=pricing.FIXED, amount=Decimal("149")
    )

    body = client.get("/admin/product/product/").content.decode()

    assert f"/admin/wsm_compose/optionset/?product__id__exact={product.pk}" in body
    assert f"/admin/wsm_compose/fee/?product__id__exact={product.pk}" in body


@pytest.mark.django_db
def test_pricing_the_same_dealer_group_twice_says_so_in_english(
    client, merchant, product
):
    """Django's message named our column: "duplicate data for tier_group"."""
    from saleor.wsm.dealer.models import DealerGroup

    client.force_login(merchant, backend=BACKEND)
    DealerGroup.objects.create(code="dealer-1", name="Dealer 1")
    option_set = OptionSet.objects.create(product=product, name="Color", label="Colour")
    value = OptionValue.objects.create(
        option_set=option_set, name="Black", price_delta=Decimal("25")
    )

    response = client.post(
        f"/admin/wsm_compose/optionvalue/{value.pk}/change/",
        {
            "option_set": option_set.pk,
            "name": "Black",
            "sku_fragment": "",
            "image_url": "",
            "sort_order": "0",
            "price_delta": "25",
            "tier_deltas-TOTAL_FORMS": "2",
            "tier_deltas-INITIAL_FORMS": "0",
            "tier_deltas-MIN_NUM_FORMS": "0",
            "tier_deltas-MAX_NUM_FORMS": "1000",
            "tier_deltas-0-tier_group": "dealer-1",
            "tier_deltas-0-price_delta": "20.00",
            "tier_deltas-1-tier_group": "dealer-1",
            "tier_deltas-1-price_delta": "21.00",
        },
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "already has a price for that dealer group" in body
    assert "duplicate data for tier_group" not in body


@pytest.mark.django_db
def test_the_fee_carriers_are_not_in_the_product_lookup(
    client, merchant, product, channel_USD
):
    """A charge owns a product only so its order line has something to hang on.

    The merchant walk found two of them in the lookup, named after the charge.
    Picking one hangs a question off something that is not on the shelf. This
    also pins the product type slug the exclusion is written against.
    """
    client.force_login(merchant, backend=BACKEND)
    fee = Fee.objects.create(
        product=product,
        label="Crating",
        basis=pricing.FIXED,
        amount=Decimal("149"),
    )
    carrier = fee.ensure_variant(channel_USD).product

    body = client.get("/admin/product/product/").content.decode()

    assert carrier.product_type.slug == "wsm-fee"
    assert product.name in body
    assert carrier.slug not in body


# --- dealer tier prices, reported on the question that owns the choice --------


def _priced_choice(option_set, name, delta, *groups):
    """A choice, with one tier row per named dealer group."""
    value = OptionValue.objects.create(
        option_set=option_set, name=name, price_delta=Decimal(delta)
    )
    for code, tier_delta in groups:
        DealerTierOptionPrice.objects.create(
            option_value=value, tier_group=code, price_delta=Decimal(tier_delta)
        )
    return value


@pytest.fixture
def two_dealer_groups(db):
    from saleor.wsm.dealer.models import DealerGroup

    return [
        DealerGroup.objects.create(code="dealer-1", name="Dealer 1"),
        DealerGroup.objects.create(code="dealer-2", name="Dealer 2"),
    ]


@pytest.mark.django_db
def test_the_question_screen_reports_each_choices_dealer_prices(
    client, merchant, product, two_dealer_groups
):
    """1,062 tier rows sit under Fuel Lab's questions and the screen said nothing.

    A merchant editing a retail price could not see that the same choice was
    priced separately for two dealer groups, so the deeper price was silently
    the one that shipped.
    """
    client.force_login(merchant, backend=BACKEND)
    option_set = OptionSet.objects.create(product=product, name="Color", label="Colour")
    priced = _priced_choice(
        option_set, "Black", "25", ("dealer-1", "-50"), ("dealer-2", "-75")
    )
    bare = _priced_choice(option_set, "Silver", "0")

    body = client.get(f"/admin/wsm_compose/optionset/{option_set.pk}/change/").content.decode()

    assert "Dealer 1 -50.00 USD" in body
    assert "Dealer 2 -75.00 USD" in body
    assert "Add dealer prices" in body
    assert f"/admin/wsm_compose/optionvalue/{priced.pk}/change/" in body
    assert f"/admin/wsm_compose/optionvalue/{bare.pk}/change/" in body


@pytest.mark.django_db
def test_the_dealer_price_column_names_the_group_not_its_code(
    client, merchant, product, two_dealer_groups
):
    """`tier_group` stores a code. A merchant knows the group by its name."""
    client.force_login(merchant, backend=BACKEND)
    option_set = OptionSet.objects.create(product=product, name="Color", label="Colour")
    _priced_choice(option_set, "Black", "25", ("dealer-1", "-50"))

    body = client.get(f"/admin/wsm_compose/optionset/{option_set.pk}/change/").content.decode()

    assert "Dealer 1 -50.00 USD" in body


@pytest.mark.django_db
def test_the_dealer_price_column_costs_no_query_per_choice(
    client, merchant, product, two_dealer_groups
):
    """The pin. One prefetch for the page, whatever the page holds.

    Three more choices and six more tier rows have to cost the same number of
    queries, or the column is a lookup per row wearing a summary's clothes.
    """
    client.force_login(merchant, backend=BACKEND)
    option_set = OptionSet.objects.create(product=product, name="Color", label="Colour")
    _priced_choice(option_set, "Black", "25", ("dealer-1", "-50"), ("dealer-2", "-75"))
    url = f"/admin/wsm_compose/optionset/{option_set.pk}/change/"
    client.get(url)  # warm anything cached per process, not per page

    with CaptureQueriesContext(connection) as one_choice:
        assert client.get(url).status_code == 200

    for name in ("Silver", "Red", "Gunmetal"):
        _priced_choice(
            option_set, name, "25", ("dealer-1", "-50"), ("dealer-2", "-75")
        )

    with CaptureQueriesContext(connection) as four_choices:
        assert client.get(url).status_code == 200

    assert len(four_choices.captured_queries) == len(one_choice.captured_queries), [
        query["sql"] for query in four_choices.captured_queries
    ]
