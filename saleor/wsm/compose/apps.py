# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
from django.apps import AppConfig
from django.contrib.auth.apps import AuthConfig
from django.db.models.signals import post_migrate

# The apps whose models a merchant is granted on. Adding the next fork app is
# adding its label here; the receiver itself never changes.
FORK_APP_LABELS = frozenset({"wsm_compose", "wsm_dealer", "wsm_containers"})


def create_wsm_permissions(sender, using=None, **kwargs):
    """Create add/change/delete/view rows for this app's models, in Saleor's table.

    Django's own `create_permissions` writes to `auth_permission`, which Saleor
    renamed to `permission_permission`; that is exactly why `WsmAuthConfig` below
    unhooks it. Something still has to create the rows a merchant is granted, so
    this does, against `saleor.permission.models.Permission`, for fork apps only.

    Every fork app, not just this one: wsm_dealer and wsm_containers put their
    screens on the AdminSite this app mounts, and a merchant who is not a
    superuser can only be granted a permission row that exists.
    """
    if sender.label not in FORK_APP_LABELS:
        return

    from django.contrib.auth import get_permission_codename
    from django.contrib.contenttypes.models import ContentType

    from ...permission.models import Permission

    using = using or "default"
    wanted = []
    for model in sender.get_models():
        opts = model._meta
        content_type = ContentType.objects.db_manager(using).get_for_model(
            model, for_concrete_model=False
        )
        for action in opts.default_permissions:
            wanted.append(
                (
                    content_type,
                    get_permission_codename(action, opts),
                    f"Can {action} {opts.verbose_name_raw}",
                )
            )

    existing = set(
        Permission.objects.using(using)
        .filter(content_type__in={ct for ct, _, _ in wanted})
        .values_list("content_type_id", "codename")
    )
    Permission.objects.using(using).bulk_create(
        Permission(content_type=ct, codename=codename, name=name)
        for ct, codename, name in wanted
        if (ct.pk, codename) not in existing
    )


class ComposeConfig(AppConfig):
    name = "saleor.wsm.compose"
    # Explicit, because Django would otherwise label this app "compose" and the
    # tables would lose the wsm_compose_ prefix that keeps them out of core's
    # namespace.
    label = "wsm_compose"
    # Two AppConfigs live in this module, so Django cannot guess which one this
    # package means.
    default = True
    verbose_name = "WSM Compose"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # No `sender`: post_migrate fires once per app and the guard above picks
        # the fork's out. Connecting per app config would mean each fork app
        # importing this one just to hook a receiver it does not own.
        post_migrate.connect(
            create_wsm_permissions,
            dispatch_uid="wsm_compose.create_wsm_permissions",
        )


class WsmAuthConfig(AuthConfig):
    """`django.contrib.auth`, minus the one thing it does that breaks on Saleor.

    The Django admin refuses to start without `django.contrib.auth` installed
    (system check admin.E405) and its templates need the auth context processor.
    But `AuthConfig.ready()` hooks `create_permissions` onto post_migrate, and
    that function queries `auth_permission`, a table Saleor's permission app
    renamed to `permission_permission` in 2022. With stock `AuthConfig`, every
    `manage.py migrate` on this branch dies with a missing relation.

    Not calling `super().ready()` also drops auth's `update_last_login` receiver
    (Saleor tracks last_login itself through the GraphQL login path) and auth's
    two model system checks. `create_wsm_permissions` above replaces the only
    behaviour we actually needed.
    """

    name = "django.contrib.auth"
    label = "django_auth"

    def ready(self):
        pass
