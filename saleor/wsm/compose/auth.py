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
"""

from django.db.models import Q

from ...account.models import User
from ...core.auth_backend import BaseBackend
from ...permission.models import Permission

WSM_APP_LABEL_PREFIX = "wsm_"


class AdminPasswordBackend(BaseBackend):
    def authenticate(self, request=None, username=None, password=None, **kwargs):
        email = username or kwargs.get("email")
        if not email or not password:
            return None
        user = User.objects.filter(email=User.objects.normalize_email(email)).first()
        if user is None:
            # Same work on a miss as on a hit, so response time does not tell an
            # attacker which addresses have accounts.
            User().set_password(password)
            return None
        if not user.check_password(password) or not user.is_active:
            return None
        return user

    def get_user(self, user_id):
        return User.objects.filter(pk=user_id, is_active=True).first()

    def _wsm_permissions(self, user_obj):
        if not getattr(user_obj, "is_active", False) or user_obj.is_anonymous:
            return set()
        cached = getattr(user_obj, "_wsm_perm_cache", None)
        if cached is None:
            rows = (
                Permission.objects.filter(
                    Q(user=user_obj) | Q(group__user=user_obj),
                    content_type__app_label__startswith=WSM_APP_LABEL_PREFIX,
                )
                .values_list("content_type__app_label", "codename")
                .distinct()
            )
            cached = {f"{app}.{codename}" for app, codename in rows}
            user_obj._wsm_perm_cache = cached
        return cached

    def get_user_permissions(self, user_obj, obj=None):
        # Per-object permissions are not a thing we grant; answering for `obj`
        # would be claiming an authority this backend does not have.
        return set() if obj is not None else self._wsm_permissions(user_obj)

    def get_group_permissions(self, user_obj, obj=None):
        # One query covers direct and group grants, so both callers get the same
        # set rather than paying twice for half of it each.
        return self.get_user_permissions(user_obj, obj=obj)
