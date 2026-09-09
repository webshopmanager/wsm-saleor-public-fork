# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The merchant's screens for series and kits.

These land on the SAME AdminSite the compose unit mounts at /admin/: one login,
one sidebar, one merchant. That site is what U2 adds to core's settings.

The collection and the variant are autocompletes over the read-only pickers in
`saleor/wsm/admin_pickers.py`: a select box that loads every collection in a
real catalog is a screen that never opens, and a bare id box with no lookup
behind it is a screen a merchant cannot use at all.

`axes` is the other half of that walk's finding. It is a JSON list on the model
because that is what the storefront and the indexer read out of the collection's
metadata, but a merchant should never be typing brackets and quotes: the form
below takes a comma-separated list of attribute slugs and refuses one that names
no attribute, which used to be a silently dead configurator question.
"""

from django import forms
from django.contrib import admin

from ...attribute.models import Attribute
from ..admin_pickers import PickerLabelMixin
from ..compose.admin import WsmAdminMixin
from ..compose.admin import site as merchant_site
from .models import KitConfig, KitMember, SeriesConfig


class SeriesConfigForm(forms.ModelForm):
    """`axes` as a comma-separated list of attribute slugs, checked against the store."""

    axes = forms.CharField(
        required=False,
        label="Axes",
        widget=forms.TextInput(attrs={"size": "60"}),
        help_text=(
            "The questions the configurator asks, in the order it asks them, as "
            "product attribute slugs separated by commas. Example: "
            "bed-length, color. Every slug must already exist as an attribute in "
            "the store."
        ),
    )

    class Meta:
        model = SeriesConfig
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # `initial` comes from `model_to_dict`, so it holds the list, not the
        # text this field edits.
        if self.instance and self.instance.pk:
            self.initial["axes"] = ", ".join(self.instance.axes or [])

    def clean_axes(self):
        slugs = []
        for chunk in self.cleaned_data["axes"].split(","):
            slug = chunk.strip()
            # A repeat is a question asked twice, never an error worth stopping
            # a merchant over.
            if slug and slug not in slugs:
                slugs.append(slug)

        known = set(
            Attribute.objects.filter(slug__in=slugs).values_list("slug", flat=True)
        )
        missing = [slug for slug in slugs if slug not in known]
        if missing:
            raise forms.ValidationError(
                "No product attribute has the slug %(missing)s. Check the "
                "attribute's slug in the Saleor dashboard, under Configuration, "
                "Attributes.",
                params={"missing": ", ".join(repr(slug) for slug in missing)},
            )
        return slugs


SERIES_DERIVED_NOTE = (
    "This screen is the only place a series is edited. Saving writes the "
    "collection's wsm.series metadata for the storefront and the search engine, "
    "and deleting a series here clears it, so the collection stops being a "
    "series everywhere. That metadata is written from this screen and is never "
    "edited by hand: anything typed into it directly is replaced the next time "
    "this form is saved."
)


class SeriesConfigAdmin(PickerLabelMixin, WsmAdminMixin, admin.ModelAdmin):
    form = SeriesConfigForm
    fieldsets = [
        (
            None,
            {
                "description": SERIES_DERIVED_NOTE,
                "fields": (
                    "collection",
                    "brand",
                    "axes",
                    "partitioning_axis",
                    "miss_message",
                    "published",
                ),
            },
        )
    ]
    list_display = ("collection_name", "brand", "axes_display", "partitioning_axis", "published")
    list_filter = ("published",)
    list_select_related = ("collection",)
    search_fields = ("brand", "collection__slug", "collection__name")
    autocomplete_fields = ("collection",)
    ordering = ("collection__name",)

    @admin.display(description="Collection", ordering="collection__name")
    def collection_name(self, obj):
        return f"{obj.collection.name} ({obj.collection.slug})"

    @admin.display(description="Axes")
    def axes_display(self, obj):
        return ", ".join(obj.axes or []) or "-"


class KitMemberInline(PickerLabelMixin, WsmAdminMixin, admin.TabularInline):
    model = KitMember
    extra = 3
    fields = ("variant", "quantity", "sort_order")
    autocomplete_fields = ("variant",)


class KitConfigAdmin(PickerLabelMixin, WsmAdminMixin, admin.ModelAdmin):
    list_display = ("collection_name", "discount_kind", "discount_amount", "active")
    list_filter = ("active", "discount_kind")
    list_select_related = ("collection",)
    search_fields = ("collection__slug", "collection__name")
    autocomplete_fields = ("collection",)
    ordering = ("collection__name",)
    inlines = [KitMemberInline]

    @admin.display(description="Collection", ordering="collection__name")
    def collection_name(self, obj):
        return f"{obj.collection.name} ({obj.collection.slug})"


def register(site):
    """Put both screens on a merchant AdminSite. One call, one place to change."""
    site.register(SeriesConfig, SeriesConfigAdmin)
    site.register(KitConfig, KitConfigAdmin)
    return site


register(merchant_site)
