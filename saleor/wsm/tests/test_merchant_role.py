# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The merchant role, and the console it opens.

READY means a merchant can operate this live as a non-superuser staff user. Two
walks of the same build produced two disjoint screenshots, because each walker
had hand-picked a different subset of about thirty permission rows: one reached
kits, series and dealer pricing, the other reached option sets and fees. There
was no packaged answer to "what is a merchant allowed to do", and the index they
landed on split one job across three headings named after our app labels.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from ...account.models import Group
from ...permission.models import Permission
from ..compose.admin import site
from ..compose.apps import FORK_APP_LABELS

pytestmark = pytest.mark.django_db


def make_role():
    out = StringIO()
    call_command("wsm_merchant_role", stdout=out)
    return out.getvalue()


def wsm_permissions():
    return Permission.objects.filter(content_type__app_label__in=FORK_APP_LABELS)


def test_the_command_grants_every_fork_permission_and_prints_the_id():
    output = make_role()

    group = Group.objects.get(name="Merchant")
    assert output.strip().endswith(str(group.pk))
    assert set(group.permissions.values_list("pk", flat=True)) == set(
        wsm_permissions().values_list("pk", flat=True)
    )
    # Four verbs on each fork model, and the fork has more than one model.
    assert group.permissions.count() >= 8


def test_it_grants_nothing_outside_the_fork():
    make_role()

    group = Group.objects.get(name="Merchant")
    granted = set(
        group.permissions.values_list("content_type__app_label", flat=True)
    )
    assert not granted - set(FORK_APP_LABELS)


def test_running_it_again_is_a_no_op():
    make_role()
    before = set(
        Group.objects.get(name="Merchant").permissions.values_list("pk", flat=True)
    )

    make_role()

    assert Group.objects.filter(name="Merchant").count() == 1
    assert set(
        Group.objects.get(name="Merchant").permissions.values_list("pk", flat=True)
    ) == before


def test_it_takes_back_a_permission_that_is_not_ours():
    """The command owns the group, so a hand-added grant does not survive it."""
    make_role()
    group = Group.objects.get(name="Merchant")
    stray = (
        Permission.objects.exclude(content_type__app_label__in=FORK_APP_LABELS)
        .order_by("pk")
        .first()
    )
    group.permissions.add(stray)

    make_role()

    assert stray.pk not in set(group.permissions.values_list("pk", flat=True))
