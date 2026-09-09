"""The fork installs `django.contrib.auth` for the admin, and it ships commands.

Django resolves a management command name to the FIRST app in INSTALLED_APPS
that ships one. `django.contrib.auth` ships `createsuperuser` and
`changepassword`, both of which `saleor.account` overrides. Installed above
`saleor.account`, auth wins and its `createsuperuser` calls `create_superuser()`
on Saleor's UserManager, which does not have it.
"""

from django.core.management import get_commands


def test_saleor_account_owns_the_user_commands():
    commands = get_commands()

    assert commands["createsuperuser"] == "saleor.account"
    assert commands["changepassword"] == "saleor.account"
