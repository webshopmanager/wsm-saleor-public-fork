# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""django.contrib.auth, installed for the admin and given no tables of its own.

Saleor renamed `auth_permission` to `permission_permission` and `auth_group` to
`account_group`, and its own `saleor.auth` shim deletes Group, Permission and
User from the `auth` label's migration state. The copy of the app we install so
the Django admin will start is relabelled `django_auth`, and its models must
never reach the database: the real rows live in Saleor's tables and are read
through `saleor.wsm.compose.auth.AdminPasswordBackend`.

An EMPTY migration is what enforces that. Pointing MIGRATION_MODULES at None
would instead mark the app unmigrated, and `migrate --run-syncdb` (which is how
the test database is built) would happily create `django_auth_permission` and
friends.
"""

from django.db import migrations


class Migration(migrations.Migration):
    initial = True
    dependencies: list = []
    operations: list = []
