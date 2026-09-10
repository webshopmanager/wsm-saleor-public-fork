# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Email-and-password sign-in for /admin/, and the fork's own permissions.

Django's `ModelBackend` cannot be used here for two independent reasons, both of
them Saleor's doing and neither of them ours to change:

1. Its permission lookups read `django.contrib.auth.models.Permission`, whose
   table `auth_permission` Saleor RENAMED to `permission_permission`
   (saleor/permission/migrations/0001_initial.py). Any permission miss would
   raise ProgrammingError instead of returning False.
2. Saleor's own `JSONWebTokenBackend` resolves permissions through
   `User.effective_permissions`, which filters to the codenames in Saleor's
   permission enum. Our `wsm_compose.*` codenames are not in that enum and never
   will be, so a merchant granted them would still be denied everything.

So this backend authenticates a password, and answers for `wsm_*` app labels
only. Saleor's backends run first and keep owning every Saleor permission.

Every read here goes to the REPLICA. This backend is consulted on every
`authenticate()` and every `has_perm()` in the process, so a read left on the
writer raises `UnsafeWriterAccessError` under the writer-restriction middleware
and takes down every authenticated request with it. A read never wants
`allow_writer`; it wants the replica.

It also honours `SiteSettings.password_login_mode`, which is the merchant's own
switch for password sign-in. Being in `AUTHENTICATION_BACKENDS` makes this
backend part of every `authenticate()` call in the process, so ignoring the
switch would have re-opened password login shop-wide for anyone with a Saleor
account, through /admin/, after the merchant turned it off.
"""

from django.conf import settings
from django.db.models import Q

from ...account.models import User
from ...core.auth_backend import BaseBackend
from ...core.db.connection import allow_writer
from ...permission.models import Permission
from ...site import PasswordLoginMode
from ...site.models import SiteSettings

WSM_APP_LABEL_PREFIX = "wsm_"
REPLICA = settings.DATABASE_CONNECTION_REPLICA_NAME


def _password_login_mode():
    """The merchant's password-login switch, or None when there is no row to read.

    Was a bare `.get()` on the replica, so a shop with no `SiteSettings` row, or
    a replica that had not caught up with a fresh install, raised
    `SiteSettings.DoesNotExist` out of `authenticate()`. This backend is in
    `AUTHENTICATION_BACKENDS`, so that was a 500 on every sign-in, staff and
    customer alike, instead of a denial.

    The WRITER is read in exactly one case: the replica has no row. That is the
    only state where lag would deny a merchant who did configure the shop, and
    it happens once, on a miss, never on the hot path. A genuine no-row shop
    still ends at None, and the caller denies: this backend cannot tell whether
    password login was turned off, and a switch it cannot read is a switch it
    treats as off.
    """
    mode = (
        SiteSettings.objects.using(REPLICA)
        .filter(site_id=settings.SITE_ID)
        .values_list("password_login_mode", flat=True)
        .first()
    )
    if mode is not None:
        return mode
    with allow_writer():
        return (
            SiteSettings.objects.filter(site_id=settings.SITE_ID)
            .values_list("password_login_mode", flat=True)
            .first()
        )


class AdminPasswordBackend(BaseBackend):
    def authenticate(self, request=None, username=None, password=None, **kwargs):
        email = username or kwargs.get("email")
        if not email or not password:
            return None
        user = (
            User.objects.using(REPLICA)
            .filter(email=User.objects.normalize_email(email))
            .first()
        )
        if user is None:
            # Same work on a miss as on a hit, so response time does not tell an
            # attacker which addresses have accounts.
            User().set_password(password)
            return None
        if not user.check_password(password) or not user.is_active:
            return None
        # The merchant's own switch, read only once a password has already been
        # proved right, so a wrong password costs no extra query and the answer
        # tells an attacker nothing new. `AUTHENTICATION_BACKENDS` is app-wide:
        # a shop that turned password login off turned it off here too.
        mode = _password_login_mode()
        if mode is None or mode == PasswordLoginMode.DISABLED:
            return None
        if mode == PasswordLoginMode.CUSTOMERS_ONLY and user.is_staff:
            return None
        return user

    def get_user(self, user_id):
        return User.objects.using(REPLICA).filter(pk=user_id, is_active=True).first()

    def _wsm_permissions(self, user_obj):
        if not getattr(user_obj, "is_active", False) or user_obj.is_anonymous:
            return set()
        cached = getattr(user_obj, "_wsm_perm_cache", None)
        if cached is None:
            rows = (
                Permission.objects.using(REPLICA)
                .filter(
                    Q(user=user_obj) | Q(group__user=user_obj),
                    content_type__app_label__startswith=WSM_APP_LABEL_PREFIX,
                )
                .values_list("content_type__app_label", "codename")
                .distinct()
            )
            cached = {f"{app}.{codename}" for app, codename in rows}
            user_obj._wsm_perm_cache = cached
        return cached

    def has_perm(self, user_obj, perm, obj=None):
        """Answer from the permission NAME, before any database read.

        This backend grants `wsm_*` and nothing else, and it sits BELOW Saleor's
        own backends in `AUTHENTICATION_BACKENDS`. Django's `_user_has_perm`
        walks the list and stops at the first backend that says yes, so every
        Saleor permission that Saleor DENIED falls through to here: an app token
        without `manage_channels`, a staff user asked for a field they cannot
        see, every optional-permission check on the API. Each one loaded our
        whole `wsm_` grant set to discover that `product.manage_products` was
        never in it, which is one `permission_permission` join bought to answer
        a question the string already answered.

        Measured as one extra query per request against upstream on
        `test_retrieve_channel_listings` (17 vs 16) and
        `test_stocks_bulk_update_queries_count` (13 vs 12); those two counts are
        the check that this stays true.

        The answers do not change, only what they cost: a non-`wsm_` permission
        was already False here, just one round trip later.
        `get_user_permissions`, `get_group_permissions` and `get_all_permissions`
        are untouched, so anything asking this backend for the SET still gets it.
        """
        if not perm.startswith(WSM_APP_LABEL_PREFIX):
            return False
        return super().has_perm(user_obj, perm, obj=obj)

    def get_user_permissions(self, user_obj, obj=None):
        # Per-object permissions are not a thing we grant; answering for `obj`
        # would be claiming an authority this backend does not have.
        return set() if obj is not None else self._wsm_permissions(user_obj)

    def get_group_permissions(self, user_obj, obj=None):
        # One query covers direct and group grants, so both callers get the same
        # set rather than paying twice for half of it each.
        return self.get_user_permissions(user_obj, obj=obj)
