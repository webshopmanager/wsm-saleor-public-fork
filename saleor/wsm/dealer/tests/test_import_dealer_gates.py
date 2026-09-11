# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The 5.0 `login_required` column, landing on the per-product gate."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ..models import DealerCategoryGate, DealerGroup, DealerProductGate

pytestmark = pytest.mark.django_db


def write(tmp_path, body, column="product_id"):
    path = tmp_path / f"gates-{column}.csv"
    path.write_text(f"{column},login_required,group_codes\n" + body)
    return str(path)


def test_it_gates_the_rows_it_is_given(tmp_path, product_list):
    a, b = product_list[0], product_list[1]
    path = write(tmp_path, f"{a.pk},1,\n{b.pk},0,\n")

    call_command("import_dealer_gates", path)

    assert DealerProductGate.objects.get(product=a).login_required is True
    assert DealerProductGate.objects.get(product=b).login_required is False


def test_it_scopes_to_the_groups_named(tmp_path, product):
    DealerGroup.objects.create(code="fob")
    DealerGroup.objects.create(code="cif")
    path = write(tmp_path, f"{product.pk},1,fob;cif\n")

    call_command("import_dealer_gates", path)

    gate = DealerProductGate.objects.get(product=product)
    assert sorted(gate.groups.values_list("code", flat=True)) == ["cif", "fob"]


def test_it_is_an_upsert(tmp_path, product):
    path = write(tmp_path, f"{product.pk},1,\n")

    call_command("import_dealer_gates", path)
    call_command("import_dealer_gates", path)

    assert DealerProductGate.objects.count() == 1


def test_a_row_it_cannot_read_stops_the_whole_import(tmp_path, product):
    """One unreadable row writes nothing at all.

    A gate guessed wrong in the permissive direction publishes a dealer price
    to the public, so nothing is written rather than most of it.
    """
    path = write(tmp_path, f"{product.pk},1,\nnot-an-id,1,\n")

    with pytest.raises(CommandError):
        call_command("import_dealer_gates", path)

    assert not DealerProductGate.objects.exists()


def test_an_unknown_group_code_stops_the_import(tmp_path, product):
    path = write(tmp_path, f"{product.pk},1,warehouse\n")

    with pytest.raises(CommandError):
        call_command("import_dealer_gates", path)

    assert not DealerProductGate.objects.exists()


def test_a_dry_run_writes_nothing(tmp_path, product):
    path = write(tmp_path, f"{product.pk},1,\n")

    call_command("import_dealer_gates", path, "--dry-run")

    assert not DealerProductGate.objects.exists()


def test_it_loads_category_gates(tmp_path, category):
    """The category door 5.0's rows come through.

    ds loses 15 category visibility rows and 5 category login gates without it.
    """
    DealerGroup.objects.create(code="jobber")
    path = write(tmp_path, f"{category.pk},1,jobber\n", column="category_id")

    call_command("import_dealer_gates", path, "--categories")

    gate = DealerCategoryGate.objects.get(category=category)
    assert gate.login_required is True
    assert list(gate.groups.values_list("code", flat=True)) == ["jobber"]


def test_an_unknown_category_stops_the_import(tmp_path):
    path = write(tmp_path, "987654,1,\n", column="category_id")

    with pytest.raises(CommandError):
        call_command("import_dealer_gates", path, "--categories")

    assert not DealerCategoryGate.objects.exists()
