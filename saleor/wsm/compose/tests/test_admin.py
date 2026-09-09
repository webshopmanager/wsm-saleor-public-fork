# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The admin, rendered as a merchant, with the writer guard switched on.

`restrict_writer_middleware` is what production runs to stop an unrouted query
reaching the writer database, and Django's admin reaches for the writer on every
page: the session, the permission rows, the LogEntry it writes on save. Without
`allow_writer` around the mount, every merchant screen answers 500 the moment
that middleware is enabled, which is the state the box is one environment
variable away from.
"""

import pytest
from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from saleor.permission.models import Permission

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
