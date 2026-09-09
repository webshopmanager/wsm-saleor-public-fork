# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The admin password backend honours the shop's own password-login setting.

`AUTHENTICATION_BACKENDS` is app-wide, so this backend is consulted on every
`authenticate()` call in the process. A merchant who turned password login off in
Saleor turned it off everywhere, including here.
"""

import pytest
from django.conf import settings as django_settings
from django.db import connections

from ....core.db.connection import restrict_writer

from ....site import PasswordLoginMode
from ..auth import AdminPasswordBackend

pytestmark = pytest.mark.django_db

PASSWORD = "s3cret-probe-password"


def _with_password(user):
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


def _mode(site_settings, mode):
    site_settings.password_login_mode = mode
    site_settings.save(update_fields=["password_login_mode"])


def test_staff_can_sign_in_when_password_login_is_enabled(staff_user, site_settings):
    _with_password(staff_user)
    _mode(site_settings, PasswordLoginMode.ENABLED)

    assert (
        AdminPasswordBackend().authenticate(
            username=staff_user.email, password=PASSWORD
        )
        == staff_user
    )


def test_staff_are_refused_when_password_login_is_disabled(staff_user, site_settings):
    _with_password(staff_user)
    _mode(site_settings, PasswordLoginMode.DISABLED)

    assert (
        AdminPasswordBackend().authenticate(
            username=staff_user.email, password=PASSWORD
        )
        is None
    )


def test_customers_are_refused_when_password_login_is_disabled(
    customer_user, site_settings
):
    _with_password(customer_user)
    _mode(site_settings, PasswordLoginMode.DISABLED)

    assert (
        AdminPasswordBackend().authenticate(
            username=customer_user.email, password=PASSWORD
        )
        is None
    )


def test_staff_are_refused_in_customers_only_mode(staff_user, site_settings):
    """The stock meaning of the mode is "a staff password buys no staff powers".

    This backend exists to open /admin/, so honouring it means refusing.
    """
    _with_password(staff_user)
    _mode(site_settings, PasswordLoginMode.CUSTOMERS_ONLY)

    assert (
        AdminPasswordBackend().authenticate(
            username=staff_user.email, password=PASSWORD
        )
        is None
    )



# --- finding 16: every read in this backend goes to the replica --------------


def test_the_backend_never_reads_the_writer(staff_user, site_settings):
    """`AUTHENTICATION_BACKENDS` is app-wide, so a read left on the writer here
    is not this app's problem: it raises `UnsafeWriterAccessError` inside every
    request that authenticates a session or asks Django for a permission, which
    is how one unrouted queryset took down a whole tenant's run.

    `execute_wrapper(restrict_writer)` is exactly what `restrict_writer_middleware`
    installs around a request, so this is the production guard, not a stand-in.
    """
    _with_password(staff_user)
    _mode(site_settings, PasswordLoginMode.ENABLED)
    backend = AdminPasswordBackend()
    writer = connections[django_settings.DATABASE_CONNECTION_DEFAULT_NAME]

    with writer.execute_wrapper(restrict_writer):
        assert (
            backend.authenticate(username=staff_user.email, password=PASSWORD)
            == staff_user
        )
        assert backend.get_user(staff_user.pk) == staff_user
        assert backend.get_user_permissions(staff_user) == set()


# --- verdict "auth.py .get() on SiteSettings 500s login" --------------------


def test_a_shop_with_no_settings_row_is_denied_rather_than_broken(
    staff_user, site_settings
):
    """The read was a bare `.get()`, so a missing row raised out of authenticate.

    This backend is in `AUTHENTICATION_BACKENDS`, so the exception did not stop
    at /admin/: every sign-in in the process, staff and customer, 500ed. A switch
    this backend cannot read is a switch it treats as off, which fails towards
    refusing a password rather than towards accepting one.
    """
    _with_password(staff_user)
    site_settings.delete()

    assert (
        AdminPasswordBackend().authenticate(
            username=staff_user.email, password=PASSWORD
        )
        is None
    )
