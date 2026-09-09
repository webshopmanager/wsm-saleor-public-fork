# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The merchant's screens. Django admin, per section 6 of the bake-off design.

Everything a merchant does to a product's configuration happens here, as a
NON-superuser staff user holding only `wsm_compose.*` permissions.

This mounts its OWN AdminSite rather than `admin.site`. Django's default site
collects a ModelAdmin from every installed app that ships one (auth's Group and
User, sites' Site), and each of those calls `user.has_module_perms()`, which
Saleor's User does not implement: every page 500s. Those models are also ones
Saleor deleted from its migration state, so there is nothing behind them worth
showing a merchant. A private site takes only what we register and is immune to
whatever a future app drops on the default one.
"""

from functools import wraps

from django.contrib import admin
from django.db.models import Count
from django.urls import URLResolver
from django.utils.html import format_html

from ...core.db.connection import allow_writer
from ...product.models import Product
from .forms import (
    DealerTierOptionPriceForm,
    FeeForm,
    OptionSetAdminForm,
    OptionValueAdminForm,
    OptionValueInlineForm,
    OptionValueInlineFormSet,
)
from .models import DealerTierOptionPrice, Fee, OptionSet, OptionValue


def _writer_view(view):
    """One admin view, allowed to use the writer connection."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        with allow_writer():
            response = view(*args, **kwargs)
            # The admin returns a lazy TemplateResponse and Django renders it
            # after the view has returned, so half the queries a page makes
            # (the index's recent-actions list, for one) would land outside
            # this block. Render it here, while the permission still holds.
            # ponytail: rendering here also skips any middleware that swaps a
            # template in process_template_response. Nothing in this stack does;
            # if one is added, render the response in a middleware instead.
            if hasattr(response, "render") and callable(response.render):
                response.render()
        return response

    return wrapper


def _allow_writer_in(patterns):
    """Wrap every view under this site, however deeply it is mounted.

    Recursive because each ModelAdmin arrives as a resolver holding its own
    patterns, and blunt because `admin_view` is not the whole site: `login` is
    mounted without it, and login is the first thing a merchant hits.
    """
    for entry in patterns:
        if isinstance(entry, URLResolver):
            _allow_writer_in(entry.url_patterns)
        elif entry.callback is not None:
            entry.callback = _writer_view(entry.callback)
    return patterns


def _cell(text):
    """One line, bounded, ellipsised, with the whole thing on hover.

    A table column is as wide as its widest cell, so two unbounded columns
    decide the width of the whole list and everything after them ends up off
    the right edge of the changelist's scroll box.
    """
    text = str(text)
    return format_html(
        '<div style="max-width: 22em; overflow: hidden; text-overflow: ellipsis;'
        ' white-space: nowrap" title="{}">{}</div>',
        text,
        text,
    )


class ComposeAdminSite(admin.AdminSite):
    site_header = "WSM Compose"
    site_title = "WSM Compose"
    index_title = "Product configuration"

    def get_urls(self):
        """Every view here runs with the writer connection explicitly allowed.

        Saleor sends reads to a replica and `restrict_writer_middleware` raises
        `UnsafeWriterAccessError` on any query that reaches the writer without
        saying so. Django's admin predates that idea by a decade: it reads the
        session and the permission rows off the default connection and writes a
        LogEntry on every save, so with the middleware on, every screen 500s.
        The whole site is a writer surface, and declaring it once here is what
        keeps the next ModelAdmin registered on it from having to know.
        """
        return _allow_writer_in(super().get_urls())


site = ComposeAdminSite(name="wsm")


class WsmAdminMixin:
    """Saleor's User has no `has_module_perms`, so the admin cannot ask for one.

    `PermissionsMixin` in saleor/permission/models.py implements `has_perm` and
    `has_perms` and stops there. Django's admin index calls
    `user.has_module_perms(app_label)` on every registered model, which would be
    an AttributeError on every page. Answering from the model's own permissions
    is what Django's default does anyway; it just spells it differently.
    """

    def has_module_permission(self, request):
        user = request.user
        if not user.is_active or not user.is_staff:
            return False
        if user.is_superuser:
            return True
        return any(self.get_model_perms(request).values())


class OptionValueInline(admin.TabularInline):
    model = OptionValue
    form = OptionValueInlineForm
    formset = OptionValueInlineFormSet
    extra = 3
    fields = ("sort_order", "name", "sku_fragment", "price_delta", "image_url")
    show_change_link = True


class DealerTierOptionPriceInline(admin.TabularInline):
    model = DealerTierOptionPrice
    form = DealerTierOptionPriceForm
    extra = 1
    fields = ("tier_group", "price_delta")


@admin.register(OptionSet, site=site)
class OptionSetAdmin(WsmAdminMixin, admin.ModelAdmin):
    """The list a merchant scans to find one question on one product.

    Product first, because that is what they are looking for. The name is
    truncated: Fuel Lab's imported names run past 60 characters and wrapped the
    column six lines deep, which pushed four rows onto a screen that should hold
    thirty. The full name is one click away, on the row.
    """

    form = OptionSetAdminForm
    list_display = ("product_name", "short_name", "value_count", "prompt_type")
    list_display_links = ("short_name",)
    list_select_related = ("product",)
    list_filter = ("prompt_type", "required")
    # Merchants look a question up by the product it is on, and support looks it
    # up by the SKU on a ticket.
    search_fields = ("name", "label", "product__name", "product__variants__sku")
    # A dropdown of every product is unusable past a few hundred SKUs, and the
    # fleet's smallest catalog is larger than that. The lookup popup below is
    # what makes this screen survive a real catalog.
    raw_id_fields = ("product",)
    inlines = [OptionValueInline]

    def get_queryset(self, request):
        # One annotated query, not one COUNT per row.
        return super().get_queryset(request).annotate(_values=Count("values"))

    @admin.display(description="Product", ordering="product__name")
    def product_name(self, obj):
        return _cell(obj.product.name)

    @admin.display(description="Question", ordering="name")
    def short_name(self, obj):
        return _cell(obj.label or obj.name)

    @admin.display(description="Choices", ordering="_values")
    def value_count(self, obj):
        return obj._values


@admin.register(OptionValue, site=site)
class OptionValueAdmin(WsmAdminMixin, admin.ModelAdmin):
    """Where a dealer tier row is added: the tier price hangs off the VALUE.

    Django cannot nest an inline inside an inline, so the tier rows live one
    click deeper, on the value they price.
    """

    form = OptionValueAdminForm
    list_display = (
        "product",
        "name",
        "sku_fragment",
        "price_delta",
        "sort_order",
        "option_set",
    )
    list_select_related = ("option_set", "option_set__product")
    search_fields = ("name", "sku_fragment", "option_set__product__name")
    raw_id_fields = ("option_set",)
    inlines = [DealerTierOptionPriceInline]

    @admin.display(description="Product", ordering="option_set__product__name")
    def product(self, obj):
        return _cell(obj.option_set.product)


@admin.register(Fee, site=site)
class FeeAdmin(WsmAdminMixin, admin.ModelAdmin):
    """A charge, in a merchant's words. See FeeForm for the labels.

    The hidden variant is created by the first configured add, never by hand, so
    it is off the form and read-only in a collapsed Internal section that
    support can open.
    """

    form = FeeForm
    list_display = (
        "product",
        "label",
        "sku",
        "charged_as",
        "amount",
        "how_often",
        "required",
    )
    list_select_related = ("product",)
    list_filter = ("basis", "apply_to", "required")
    search_fields = ("label", "sku", "product__name")
    raw_id_fields = ("product",)
    readonly_fields = ("variant",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "product",
                    "label",
                    "sku",
                    "basis",
                    "amount",
                    "apply_to",
                    "required",
                    "decline_label",
                )
            },
        ),
        (
            "Internal",
            {
                "classes": ("collapse",),
                "fields": ("variant",),
                "description": ("Support only. Nothing here is edited by hand."),
            },
        ),
    )

    @admin.display(description="Charged as", ordering="basis")
    def charged_as(self, obj):
        return obj.get_basis_display()

    @admin.display(description="How often", ordering="apply_to")
    def how_often(self, obj):
        return obj.get_apply_to_display()


@admin.register(Product, site=site)
class ComposeProductPickerAdmin(WsmAdminMixin, admin.ModelAdmin):
    """Read-only product list, so the option-set lookup popup resolves.

    Registered because `raw_id_fields` needs a changelist to point at, not
    because products are edited here: Saleor's own Dashboard owns the catalog.
    Every write is refused regardless of what the user was granted.
    """

    list_display = ("name", "slug", "product_type")
    search_fields = ("name", "slug", "variants__sku")
    ordering = ("name",)

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_view_permission(self, request, obj=None):
        # Anyone who may look at an option set may look up the product it is on.
        return request.user.has_perm("wsm_compose.view_optionset")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
