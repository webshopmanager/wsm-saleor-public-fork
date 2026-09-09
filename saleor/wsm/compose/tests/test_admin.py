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
from saleor.wsm.compose.models import Fee, OptionSet, OptionValue

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


