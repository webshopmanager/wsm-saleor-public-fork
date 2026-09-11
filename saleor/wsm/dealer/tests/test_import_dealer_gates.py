# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The 5.0 `login_required` column, landing on the per-product gate."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ..models import DealerGroup, DealerProductGate

pytestmark = pytest.mark.django_db


def write(tmp_path, body):
    path = tmp_path / "gates.csv"
    path.write_text("product_id,login_required,group_codes\n" + body)
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
    """A gate guessed wrong in the permissive direction publishes a dealer
    price to the public, so nothing is written rather than most of it."""
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
