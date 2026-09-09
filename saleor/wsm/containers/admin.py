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
metadata, but a merchant should never be typing brackets and quotes, and the
walk of 2026-09-08 found that typing them into a free-text box meant looking the
slugs up in a DIFFERENT application first. The form below offers the store's own
product attributes, by name, and writes the same JSON list back.
"""

from django import forms
from django.contrib import admin
from django.db.models import Count, Q

from ...attribute import AttributeType
from ...attribute.models import Attribute
from ...product.models import Product
from ..admin_pickers import PickerLabelMixin
from ..compose.admin import EmptyStateMixin, WsmAdminMixin
from ..compose.admin import site as merchant_site
from . import pricing
from .models import KitConfig, KitMember, SeriesConfig

MISSING = "(missing)"


def axis_choices(held):
    """Every product attribute this store has, named, plus any slug it has lost.

    ONE query. `held` is what the row already carries: a slug that no longer
    names an attribute is offered anyway, marked, because a form that silently
    dropped it would delete a configurator question the merchant never asked to
    lose and would say nothing about it.
    """
    known = list(
        Attribute.objects.filter(type=AttributeType.PRODUCT_TYPE)
        .order_by("name")
        .values_list("slug", "name")
    )
    choices = [(slug, f"{name} ({slug})") for slug, name in known]
    slugs = {slug for slug, _ in known}
    choices.extend((slug, f"{slug} {MISSING}") for slug in held if slug not in slugs)
    return choices


class SeriesConfigForm(forms.ModelForm):
    """`axes` picked from the store's own attributes, stored as the same JSON list.

    ponytail: the order the questions are asked is now the order the choices are
    listed in, alphabetically by attribute name, and a merchant cannot reorder
    them. That is the ceiling of a checkbox list. It is deliberate over a free
    text box that could order them but sent a merchant to another application to
    find out what to type, and over a silent reorder: what the screen shows is
    what is saved. Upgrade path if a merchant asks for an order: an ordered
    widget, which is JavaScript, on this one field.
    """

    axes = forms.MultipleChoiceField(
        required=False,
        label="Questions the configurator asks",
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Tick every question this series asks. They are asked in the order "
            "listed here. An entry marked (missing) is one this series still "
            "holds that the store no longer has an attribute for: untick it to "
            "drop it."
        ),
    )
    partitioning_axis = forms.ChoiceField(
        label="The question that decides which product",
    )

    class Meta:
        model = SeriesConfig
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        held = list(self.instance.axes or []) if self.instance.pk else []
        if self.instance.pk and self.instance.partitioning_axis:
            held.append(self.instance.partitioning_axis)
        choices = axis_choices(held)
        self.fields["axes"].choices = choices
        # Never a pre-selected first attribute: an empty choice is what makes
        # "required" mean the merchant chose, rather than the merchant not
        # noticing. The model's own rules still decide whether the choice is a
        # legal one for a published series.
        self.fields["partitioning_axis"].choices = [("", "---------"), *choices]
        self.fields["partitioning_axis"].help_text = SeriesConfig._meta.get_field(
            "partitioning_axis"
        ).help_text
        if self.instance.pk:
            self.initial["axes"] = list(self.instance.axes or [])

    def clean_axes(self):
        """Back to a JSON list, in the order the merchant was shown."""
        chosen = set(self.cleaned_data["axes"])
        return [slug for slug, _ in self.fields["axes"].choices if slug in chosen]


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
    readonly_fields = ("member_count",)
    fieldsets = [
        (
            None,
            {
                "description": SERIES_DERIVED_NOTE,
                "fields": (
                    "collection",
                    "member_count",
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

    @admin.display(description="Products in this series")
    def member_count(self, obj):
        """One aggregate, and it answers the publish rule before it refuses.

        A merchant reads "publishing is refused unless 2 or more members are
        published" on the field below and then has no way to find out how many
        there are without leaving for the Saleor dashboard.
        """
        if obj is None or obj.pk is None:
            return (
                "Pick a collection and save. The products come from the "
                "collection, which is managed in the Saleor dashboard."
            )
        counts = Product.objects.filter(collections__id=obj.collection_id).aggregate(
            total=Count("pk", distinct=True),
            published=Count(
                "pk", filter=Q(channel_listings__is_published=True), distinct=True
            ),
        )
        return (
            f"{counts['total']} in the collection, {counts['published']} published. "
            "Add or remove products on the collection, in the Saleor dashboard."
        )


class KitMemberInline(PickerLabelMixin, WsmAdminMixin, admin.TabularInline):
    model = KitMember
    extra = 3
    fields = ("variant", "quantity", "sort_order")
    autocomplete_fields = ("variant",)


class KitConfigAdmin(
    EmptyStateMixin, PickerLabelMixin, WsmAdminMixin, admin.ModelAdmin
):
    empty_state = (
        "No kits yet.",
        "A kit is a collection of products sold as one, priced at the sum of "
        "its parts or at a saving you set.",
        "Add the first one",
    )
    list_display = ("collection_name", "saving", "active")
    list_filter = ("active", "discount_kind")
    list_select_related = ("collection",)
    search_fields = ("collection__slug", "collection__name")
    autocomplete_fields = ("collection",)
    ordering = ("collection__name",)
    inlines = [KitMemberInline]

    @admin.display(description="Collection", ordering="collection__name")
    def collection_name(self, obj):
        return f"{obj.collection.name} ({obj.collection.slug})"

    @admin.display(description="Saving", ordering="discount_amount")
    def saving(self, obj):
        """Two columns said "fixed" and "0.00"; one column says what that means."""
        if not obj.discount_amount:
            return "None (sells at the sum of its parts)"
        amount = f"{obj.discount_amount:.2f}"
        if obj.discount_kind == pricing.PERCENT:
            # The column is read, not summed: 10.00% is two characters of noise.
            return f"{amount.rstrip('0').rstrip('.')}% off the kit"
        return f"{amount} off the kit"


def register(site):
    """Put both screens on a merchant AdminSite. One call, one place to change."""
    site.register(SeriesConfig, SeriesConfigAdmin)
    site.register(KitConfig, KitConfigAdmin)
    return site


register(merchant_site)
