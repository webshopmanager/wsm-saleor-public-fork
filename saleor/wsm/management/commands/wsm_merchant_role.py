# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The merchant role as one command, so READY is reproducible.

The bar is "a merchant can operate it live from /admin/ as a non-superuser
staff user". It had been proved twice, on two DIFFERENT partial users: one
reached kits, series and dealer pricing, the other reached option sets and
fees, and neither could do the whole job. Nothing under `saleor/wsm/` created a
group, so provisioning a merchant meant hand-picking about thirty permission
rows by hand, differently every time.

The group is `saleor.account.Group`, which IS the Django auth group on this
deployment: Saleor renamed `auth_group` to `account_group` and `auth_permission`
to `permission_permission` in 2022, and `AdminPasswordBackend` answers a
merchant's `wsm_*` permissions out of those two tables.

Idempotent both ways. The command owns the group's contents: every add, change,
delete and view permission on every fork model goes on, and anything else found
on the group comes off, so a second run after a new fork model is added is the
whole update and a second run after nothing changed is a no-op.
"""

from django.core.management.base import BaseCommand, CommandError

from ....account.models import Group
from ....permission.models import Permission
from ...compose.apps import FORK_APP_LABELS

GROUP_NAME = "Merchant"


class Command(BaseCommand):
    help = (
        "Create or update the staff group that can operate the whole WSM "
        "merchant console, and print its id."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            default=GROUP_NAME,
            help=f"Group name to create or update. Default: {GROUP_NAME}.",
        )

    def handle(self, *args, **options):
        wanted = list(
            Permission.objects.filter(
                content_type__app_label__in=sorted(FORK_APP_LABELS)
            )
        )
        if not wanted:
            # The rows are written by `create_wsm_permissions` on post_migrate,
            # so an empty set means migrate has not run, not that the fork has
            # no models. Granting nothing silently would look like success.
            raise CommandError(
                "No wsm permissions exist yet. Run migrate first: they are "
                "created by saleor.wsm.compose.apps.create_wsm_permissions."
            )

        group, created = Group.objects.get_or_create(name=options["name"])
        before = set(group.permissions.values_list("pk", flat=True))
        group.permissions.set(wanted)
        after = {permission.pk for permission in wanted}

        self.stdout.write(
            "{} group {} ({!r}) with {} permissions across {}.".format(
                "Created" if created else "Updated",
                group.pk,
                group.name,
                len(wanted),
                ", ".join(sorted(FORK_APP_LABELS)),
            )
        )
        if not created:
            self.stdout.write(
                f"  added {len(after - before)}, removed {len(before - after)}"
            )
        self.stdout.write(str(group.pk))
        return
