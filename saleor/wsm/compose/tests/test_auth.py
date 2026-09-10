# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The admin password backend honours the shop's own password-login setting.

`AUTHENTICATION_BACKENDS` is app-wide, so this backend is consulted on every
`authenticate()` call in the process. A merchant who turned password login off in
Saleor turned it off everywhere, including here.
"""

import pytest
from django.conf import settings as django_settings
from django.db import connections
from django.test.utils import CaptureQueriesContext

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


# --- a permission this backend cannot grant costs nothing to refuse ----------
#
# Counted on the DEFAULT connection, which is where a `.using(replica)` read
# lands under `saleor.tests.settings`: the replica alias is a test mirror of it,
# and it is the connection `django_assert_num_queries` watches, so these two
# counts are the same arithmetic as the benchmark counts they exist to hold up.


def _reads_while(call) -> int:
    connection = connections[django_settings.DATABASE_CONNECTION_DEFAULT_NAME]
    with CaptureQueriesContext(connection) as queries:
        call()
    return len(queries)


def test_a_saleor_permission_is_refused_without_a_query(staff_user):
    """`AUTHENTICATION_BACKENDS` is app-wide and this backend is last in it, so
    every Saleor permission Saleor itself denied is asked of us next. Loading the
    `wsm_` grant set to answer for `product.manage_products` was one join per
    request on the API's hot path, and the string already carries the answer.

    That one query is the whole gap between this fork and upstream on
    `test_retrieve_channel_listings` (17 vs 16) and
    `test_stocks_bulk_update_queries_count` (13 vs 12).
    """
    backend = AdminPasswordBackend()
    answer = []

    reads = _reads_while(
        lambda: answer.append(backend.has_perm(staff_user, "product.manage_products"))
    )

    assert answer == [False]
    assert reads == 0


def test_a_wsm_permission_is_still_read_from_the_grant(staff_user):
    """The other half: the permissions this backend DOES own still cost their one
    read, so the short-circuit above cannot be widened into refusing everything.
    """
    backend = AdminPasswordBackend()
    answer = []

    reads = _reads_while(
        lambda: answer.append(backend.has_perm(staff_user, "wsm_compose.change_fee"))
    )

    assert answer == [False]
    assert reads == 1


# --- /admin/ sign-in runs through Saleor's own throttle ---------------------

LOGIN_URL = "/admin/login/"


def _sign_in(client, email, password):
    return client.post(LOGIN_URL, {"username": email, "password": password})


@pytest.fixture
def clean_throttle_cache():
    """The throttle counts in the process cache, which outlives one test."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def test_a_failed_admin_sign_in_blocks_the_next_attempt_from_the_same_address(
    client, staff_user, site_settings, clean_throttle_cache
):
    """The fork's password door is limited by the limiter Saleor already owns.

    Without this the door costs a full PBKDF2 hash per POST, hit or miss (the
    miss hashes on purpose, so timing does not say which addresses exist), with
    nothing counting the attempts. Measured at ~2.1s of CPU each on the bake-off
    box, against one 256-CPU task in prod.
    """
    from django.core.cache import cache
    from ....account.throttling import get_cache_key_blocked_ip

    _with_password(staff_user)
    _mode(site_settings, PasswordLoginMode.ENABLED)

    first = _sign_in(client, staff_user.email, "wrong-password")
    assert first.status_code == 200
    assert cache.get(get_cache_key_blocked_ip("127.0.0.1")) is not None, (
        "a failed admin sign-in was not counted by the throttle"
    )

    second = _sign_in(client, staff_user.email, "wrong-password")
    assert second.status_code == 200
    assert b"suspended" in second.content, (
        "the second attempt was answered rather than refused"
    )
    # And the refusal is a refusal, not a slow yes: the right password inside
    # the block does not get in either.
    third = _sign_in(client, staff_user.email, PASSWORD)
    assert b"suspended" in third.content
    assert third.wsgi_request.user.is_anonymous


def test_a_correct_password_still_gets_the_merchant_in(
    rf, staff_user, site_settings, clean_throttle_cache
):
    """The form, not the round trip.

    A successful POST to /admin/login/ cannot be asserted end to end under the
    test harness: the fork's /admin/-scoped session middleware saves the session
    in `process_response`, outside the `allow_writer` the admin site wraps its
    VIEWS in, so `restrict_writer` refuses it. That is true at 23d393e3 as well,
    with this form and without it, and `restrict_writer_middleware` is not in
    the running app's MIDDLEWARE, so it is a harness artifact today and a real
    one the day it is turned on. Filed, not fixed here.
    """
    from ..auth import ThrottledAdminAuthenticationForm

    _with_password(staff_user)
    _mode(site_settings, PasswordLoginMode.ENABLED)
    request = rf.post("/admin/login/")

    form = ThrottledAdminAuthenticationForm(
        request, data={"username": staff_user.email, "password": PASSWORD}
    )

    assert form.is_valid(), form.errors
    assert form.get_user() == staff_user
    # `authenticate()` normally records which backend answered, and
    # `django.contrib.auth.login` refuses a user without it.
    assert form.get_user().backend.endswith("AdminPasswordBackend")


def test_the_admin_door_still_honours_the_shops_password_login_switch(
    client, staff_user, site_settings, clean_throttle_cache
):
    """The throttle knows nothing about the merchant's switch; the door still does."""
    _with_password(staff_user)
    _mode(site_settings, PasswordLoginMode.DISABLED)

    response = _sign_in(client, staff_user.email, PASSWORD)

    assert response.status_code == 200
    assert response.wsgi_request.user.is_anonymous
