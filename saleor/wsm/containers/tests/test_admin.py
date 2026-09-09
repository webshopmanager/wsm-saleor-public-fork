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
        "axes": "color",
        "partitioning_axis": "color",
        "miss_message": "",
    }
    data.update(overrides)
    return data


# --- axes as a list of slugs, not JSON ---------------------------------------


def test_the_axes_field_refuses_a_slug_that_names_no_attribute(
    collection, color_attribute
):
    """A typo'd axis is a configurator question that can never be answered."""
    form = form_class()(data=series_data(collection, axes="color, cab-style"))

    assert not form.is_valid()
    assert "cab-style" in str(form.errors["axes"])
    assert "color" not in str(form.errors["axes"])


def test_the_axes_field_takes_a_comma_separated_list(collection, color_attribute):
    form = form_class()(data=series_data(collection, axes=" color , color "))

    assert form.is_valid(), form.errors
    assert form.cleaned_data["axes"] == ["color"]
    assert form.save().axes == ["color"]


def test_the_axes_field_shows_the_saved_list_back_as_text(collection, color_attribute):
    series = SeriesConfig.objects.create(
        collection=collection, brand="WeatherTech", axes=["color"], partitioning_axis="color"
    )

    assert form_class()(instance=series).initial["axes"] == "color"


def test_the_published_gate_still_reaches_the_form(
    collection, product_list, color_attribute
):
    """The refusal the walk liked stays a field error, not a silent no-op."""
    collection.products.add(*product_list)
    form = form_class()(
        data=series_data(collection, partitioning_axis="finish", published="on")
    )

    assert not form.is_valid()
    assert "not one of the axes" in str(form.errors["partitioning_axis"])


def test_an_unpublished_series_is_never_gated(collection, color_attribute):
    """Nothing stops a merchant building a series before it has members."""
    form = form_class()(data=series_data(collection))

    assert form.is_valid(), form.errors


# --- the screens --------------------------------------------------------------


def test_the_series_add_form_is_a_text_field_with_help(merchant):
    response = merchant.get(SERIES_ADD)

    assert response.status_code == 200
    body = response.content.decode()
    assert 'name="axes"' in body
    assert '<textarea name="axes"' not in body
    assert "separated by commas" in body
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
