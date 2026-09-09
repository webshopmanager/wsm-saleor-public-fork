# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Can a merchant operate the series and kit screens?

The merchant walk of 2026-09-08 found `axes` as a raw JSON textarea, a
collection field with no lookup behind it, and no help text saying what
`partitioning_axis`, `miss_message` or `published` do. The client below is a
STAFF NON-SUPERUSER holding only the model permissions.
"""

import pytest

from ....permission.models import Permission
from ..models import SeriesConfig

pytestmark = pytest.mark.django_db

AUTH_BACKEND = "saleor.wsm.compose.auth.AdminPasswordBackend"
SERIES_ADD = "/admin/wsm_containers/seriesconfig/add/"
KIT_ADD = "/admin/wsm_containers/kitconfig/add/"
AUTOCOMPLETE = "/admin/autocomplete/"


def grant(user, *dotted_permissions):
    for dotted in dotted_permissions:
        app_label, codename = dotted.split(".")
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        )


@pytest.fixture(autouse=True)
def deployed_middleware(settings):
    """Run the admin the way the box runs it.

    `saleor/tests/settings.py` inserts `restrict_writer_middleware` AHEAD of the
    session middleware, so under the test settings every admin request 500s on
    its own session read: the Django admin reads the session, the user and its
    permissions from the default connection by design, before any view runs.
    The deployed setting is `ENABLE_RESTRICT_WRITER_MIDDLEWARE` off, which is
    what these tests therefore run under. Making /admin/ survive that middleware
    would take an `allow_writer` around the whole mount in wsm.compose's
    AdminSite; it is a finding on that file, not something these screens can fix.
    """
    settings.MIDDLEWARE = [
        middleware
        for middleware in settings.MIDDLEWARE
        if "restrict_writer" not in middleware
    ]

@pytest.fixture
def merchant(client, staff_user):
    assert staff_user.is_staff and not staff_user.is_superuser
    grant(
        staff_user,
        "wsm_containers.view_seriesconfig",
        "wsm_containers.add_seriesconfig",
        "wsm_containers.change_seriesconfig",
        "wsm_containers.view_kitconfig",
        "wsm_containers.add_kitconfig",
        "wsm_containers.change_kitconfig",
        "wsm_containers.view_kitmember",
        "wsm_containers.add_kitmember",
        "wsm_containers.change_kitmember",
    )
    client.force_login(staff_user, backend=AUTH_BACKEND)
    return client


def form_class():
    from ..admin import SeriesConfigForm

    return SeriesConfigForm


def series_data(collection, **overrides):
    data = {
        "collection": collection.pk,
        "brand": "WeatherTech",
        "axes": ["color"],
        "partitioning_axis": "color",
        "miss_message": "",
    }
    data.update(overrides)
    return data


# --- axes picked from the store's own attributes ------------------------------


def test_the_axes_field_offers_the_stores_attributes_by_name(
    collection, color_attribute
):
    """The walk's finding: a free-text box sent a merchant to another app first.

    The screen now offers what the store has, named the way the store names it,
    with the slug alongside because the slug is what the storefront reads.
    """
    choices = dict(form_class()().fields["axes"].choices)

    assert choices["color"] == f"{color_attribute.name} (color)"


def test_the_axes_field_refuses_a_slug_that_names_no_attribute(
    collection, color_attribute
):
    """A typo'd axis is a configurator question that can never be answered."""
    form = form_class()(data=series_data(collection, axes=["color", "cab-style"]))

    assert not form.is_valid()
    assert "cab-style" in str(form.errors["axes"])


def test_the_axes_field_saves_the_ticked_questions_as_a_json_list(
    collection, color_attribute, size_attribute
):
    """The storefront and the indexer read a JSON list. That does not change."""
    form = form_class()(
        data=series_data(collection, axes=["size", "color"], partitioning_axis="color")
    )

    assert form.is_valid(), form.errors
    # The order the merchant was SHOWN, so what the screen said is what saved.
    assert form.cleaned_data["axes"] == ["color", "size"]
    assert form.save().axes == ["color", "size"]


def test_a_saved_series_shows_its_questions_already_ticked(
    collection, color_attribute
):
    series = SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color"],
        partitioning_axis="color",
    )

    assert form_class()(instance=series).initial["axes"] == ["color"]


def test_a_question_whose_attribute_is_gone_is_offered_and_marked(collection):
    """Never silently drop a question the merchant did not ask to lose.

    A slug can outlive its attribute: the attribute is deleted or renamed in the
    Saleor dashboard and this row still holds it. A form that offered only what
    exists would delete the question on the next save and say nothing.
    """
    series = SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["cab-style"],
        partitioning_axis="cab-style",
    )

    form = form_class()(instance=series)
    choices = dict(form.fields["axes"].choices)

    assert choices["cab-style"] == "cab-style (missing)"
    assert form.initial["axes"] == ["cab-style"]
    assert "cab-style" in dict(form.fields["partitioning_axis"].choices)


def test_the_published_gate_still_reaches_the_form(
    collection, product_list, color_attribute, size_attribute
):
    """The refusal the walk liked stays a field error, not a silent no-op."""
    collection.products.add(*product_list)
    form = form_class()(
        data=series_data(collection, partitioning_axis="size", published="on")
    )

    assert not form.is_valid()
    assert "not one of the axes" in str(form.errors["partitioning_axis"])


def test_an_unpublished_series_is_never_gated(collection, color_attribute):
    """Nothing stops a merchant building a series before it has members."""
    form = form_class()(data=series_data(collection))

    assert form.is_valid(), form.errors


# --- how many products a series actually has ---------------------------------


def test_the_series_screen_counts_the_products_in_the_collection(
    merchant, collection, product_list, color_attribute
):
    """Publishing is refused under 2 published members and the screen said
    nothing about how many there were, so the merchant had to leave to find out.
    """
    collection.products.add(*product_list)
    series = SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color"],
        partitioning_axis="color",
    )

    body = merchant.get(
        f"/admin/wsm_containers/seriesconfig/{series.pk}/change/"
    ).content.decode()

    assert f"{len(product_list)} in the collection, {len(product_list)} published" in body


def test_the_count_tells_an_unsaved_series_where_products_come_from(merchant):
    body = merchant.get(SERIES_ADD).content.decode()

    assert "Pick a collection and save" in body


# --- the screens --------------------------------------------------------------


def test_the_series_add_form_ticks_attributes_instead_of_typing_slugs(
    merchant, color_attribute
):
    response = merchant.get(SERIES_ADD)

    assert response.status_code == 200
    body = response.content.decode()
    assert '<textarea name="axes"' not in body
    assert 'type="checkbox" name="axes" value="color"' in body
    assert f"{color_attribute.name} (color)" in body
    assert "Tick every question this series asks" in body
    assert "one of the axes above" in body


def test_the_kit_add_form_explains_the_discount(merchant):
    body = merchant.get(KIT_ADD).content.decode()

    assert "amount off the whole" in body
    assert "How many of this SKU one kit contains" in body


def test_the_collection_picker_answers_the_autocomplete(merchant, collection):
    response = merchant.get(
        AUTOCOMPLETE,
        {
            "app_label": "wsm_containers",
            "model_name": "seriesconfig",
            "field_name": "collection",
            "term": collection.name,
        },
    )

    assert response.status_code == 200
    texts = [result["text"] for result in response.json()["results"]]
    assert texts == [f"{collection.name} ({collection.slug})"]


def test_the_series_form_says_the_blob_is_derived(merchant):
    """A merchant reading the screen learns the metadata is output, not an input."""
    body = merchant.get(SERIES_ADD).content.decode()

    assert "only place a series is edited" in body
    assert "never edited by hand" in body


def test_the_admin_delete_action_clears_the_blob(
    merchant, staff_user, collection, product_list, color_attribute
):
    """The merchant's bulk delete is the third door onto the same rule.

    `delete_selected` goes through `ModelAdmin.delete_queryset`, so the model's
    own `delete()` is never called and the clear has to live on the queryset.
    """
    from ..models import SERIES_METADATA_KEY

    grant(staff_user, "wsm_containers.delete_seriesconfig")
    collection.products.add(*product_list[:2])
    series = SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color"],
        partitioning_axis="color",
    )
    collection.refresh_from_db()
    assert SERIES_METADATA_KEY in collection.metadata

    response = merchant.post(
        "/admin/wsm_containers/seriesconfig/",
        {
            "action": "delete_selected",
            "_selected_action": [str(series.pk)],
            "post": "yes",
        },
    )

    assert response.status_code == 302, response.status_code
    assert not SeriesConfig.objects.filter(pk=series.pk).exists()
    collection.refresh_from_db()
    assert SERIES_METADATA_KEY not in collection.metadata


def test_the_series_list_names_the_collection_and_the_axes(
    merchant, collection, color_attribute
):
    SeriesConfig.objects.create(
        collection=collection,
        brand="WeatherTech",
        axes=["color"],
        partitioning_axis="color",
    )

    body = merchant.get("/admin/wsm_containers/seriesconfig/").content.decode()

    assert collection.slug in body
    assert "WeatherTech" in body


# --- what the screens say, in the merchant's words ---------------------------


def test_the_kit_discount_kinds_are_named_not_coded(collection):
    """The list said "fixed", which is our enum, next to "0.00", which is money.

    A merchant reading a kit row could not tell a kit that gives nothing away
    from one that takes ten percent off.
    """
    from ..models import KitConfig

    labels = dict(KitConfig._meta.get_field("discount_kind").choices)

    assert labels["fixed"] == "An amount off the whole kit"
    assert labels["percent"] == "A percentage off the whole kit"


def test_a_kit_row_says_what_it_takes_off(merchant, collection):
    from decimal import Decimal

    from ....product.models import Collection

    from ..models import KitConfig

    KitConfig.objects.create(
        collection=collection, discount_kind="percent", discount_amount=Decimal("10")
    )

    KitConfig.objects.create(
        collection=Collection.objects.create(name="Crate kit", slug="crate-kit"),
        discount_kind="fixed",
        discount_amount=Decimal("25"),
    )

    body = merchant.get("/admin/wsm_containers/kitconfig/").content.decode()

    assert "10% off the kit" in body
    assert "25.00 off the kit" in body
    assert ">fixed<" not in body


def test_the_axes_help_no_longer_sends_the_merchant_to_another_application(
    merchant, color_attribute
):
    """The old help was a set of directions to the Saleor dashboard.

    It read well and it still cost the merchant a trip to a second application
    to find out what to type. The choices are on this screen now, so the field
    has nothing left to send anyone anywhere for.
    """
    help_text = str(form_class()().fields["axes"].help_text)

    assert "Configuration, Attributes" not in help_text
    assert "slug" not in help_text.lower()


def test_an_empty_kit_list_says_what_a_kit_is(merchant):
    """The walk's screenshot of this list read "0 kits" and nothing else."""
    body = merchant.get("/admin/wsm_containers/kitconfig/").content.decode()

    assert "No kits yet." in body
    assert "sold as one" in body
    assert 'href="/admin/wsm_containers/kitconfig/add/"' in body


def test_a_kit_list_with_a_kit_in_it_is_the_ordinary_list(merchant, collection):
    from ..models import KitConfig

    KitConfig.objects.create(collection=collection)

    body = merchant.get("/admin/wsm_containers/kitconfig/").content.decode()

    assert "No kits yet." not in body
    assert collection.name in body
