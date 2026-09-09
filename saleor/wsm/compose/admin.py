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
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Subquery
from django.urls import URLResolver, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from ...core.db.connection import allow_writer
from ...product.models import Product, ProductChannelListing
from .forms import (
    DealerTierOptionPriceForm,
    DealerTierOptionPriceFormSet,
    FeeForm,
    OptionSetAdminForm,
    OptionValueAdminForm,
    OptionValueInlineForm,
    OptionValueInlineFormSet,
    label_money_field,
    money,
)
from . import pricing
from .models import DealerTierOptionPrice, Fee, OptionSet, OptionValue


# The merchant console mounts on the same host as the public API, so
# `/admin/login/` is a crawlable 200. One header on the way out keeps the whole
# site out of an index, login page included, without a robots.txt Disallow
# publishing the path to anyone who reads it.
NOINDEX = "noindex, nofollow"


def _writer_view(view):
    """One admin view, allowed to use the writer connection and never indexed."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        with allow_writer():
            response = view(*args, **kwargs)
            response["X-Robots-Tag"] = NOINDEX
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


def _related_count(model):
    """How many rows of `model` point at the product this row is."""
    return Subquery(
        model.objects.filter(product_id=OuterRef("pk"))
        .order_by()
        .values("product_id")
        .annotate(n=Count("pk"))
        .values("n")[:1],
        output_field=IntegerField(),
    )


def _with_currency(queryset, product_path):
    """Carry the product's channel currency on the row, not one query per row.

    A money column with no currency on it is the defect; asking per row is the
    expensive way to fix it. This is a correlated subquery on the query the
    changelist already runs, so the page cost does not move.
    """
    return queryset.annotate(
        wsm_currency=Subquery(
            ProductChannelListing.objects.filter(
                product_id=OuterRef(product_path)
            ).values("currency")[:1]
        )
    )


class ProductFilteredMixin:
    """A changelist that can be opened for ONE product, from the product itself.

    Django refuses a changelist query parameter that no `list_filter` declares,
    and the honest `list_filter` here would be a dropdown of every product in
    the catalog: the screen the pickers exist to avoid. Allowing the one exact
    lookup keeps the sidebar empty and the link working.
    """

    def lookup_allowed(self, lookup, value, request=None):
        if lookup == "product__id__exact":
            return True
        return super().lookup_allowed(lookup, value, request)

    def product_url(self, product):
        """The read-only product row, found the way the picker finds anything."""
        return "{}?q={}".format(
            reverse(f"{self.admin_site.name}:product_product_changelist"),
            product.slug,
        )


class ComposeAdminSite(admin.AdminSite):
    site_header = "WSM Compose"
    site_title = "WSM Compose"
    index_title = "Product configuration"

    # The order a merchant works in, not the order three AppConfigs happen to
    # be registered in. Three apps is an implementation fact: the `wsm_` table
    # prefixes depend on the labels, and the merchant's job never did. Anything
    # registered and not named here still appears, at the end, so a new screen
    # is never invisible.
    merchant_order = (
        ("wsm_compose", "OptionSet"),
        ("wsm_compose", "Fee"),
        ("wsm_dealer", "DealerGroup"),
        ("wsm_dealer", "DealerCustomer"),
        ("wsm_dealer", "TierPrice"),
        ("wsm_dealer", "DealerSettings"),
        ("wsm_containers", "KitConfig"),
        ("wsm_containers", "SeriesConfig"),
    )

    def get_app_list(self, request, app_label=None):
        """One group, in task order, whoever the merchant is.

        The stock index groups by app, so this console showed WSM COMPOSE, WSM
        CONTAINERS and WSM DEALER PRICING as three unrelated headings, and which
        of them a merchant saw depended on which permissions they happened to
        hold: two walks of the same build produced two disjoint screenshots and
        no way to tell a permission gap from a missing feature.

        `app_label` is passed only by the per-app index page, which is a page
        about one app and is left alone.
        """
        if app_label is not None:
            return super().get_app_list(request, app_label)

        models = []
        for app in self._build_app_dict(request).values():
            models.extend(app["models"])
        if not models:
            return []

        rank = {pair: i for i, pair in enumerate(self.merchant_order)}
        for entry in models:
            opts = entry["model"]._meta
            entry["wsm_rank"] = rank.get(
                (opts.app_label, entry["model"].__name__), len(rank)
            )
            if opts.app_label == "product":
                # Registered so a lookup has somewhere to point, and read-only.
                # Saying so stops it reading as a second "Products" screen.
                entry["name"] = "Products (catalog lookup)"
        models.sort(key=lambda entry: (entry["wsm_rank"], entry["name"]))
        return [
            {
                "name": "Products",
                "app_label": "wsm",
                "app_url": reverse(f"{self.name}:index", current_app=self.name),
                "has_module_perms": True,
                "models": models,
            }
        ]

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


# The 5.0 import brings the shopper-facing help across as it was written, and
# 118 of Fuel Lab's 126 questions carry markup in it. The storefront renders it
# as HTML, so the stored text is right and the SCREEN was the defect: a merchant
# opening the field saw `<strong>Color</strong>` with nothing saying whether the
# shopper would see bold text or those characters. Written with entities because
# Django renders `help_text` unescaped, and this sentence has to SHOW the tags.
NOTE_IS_HTML = (
    "HTML is allowed here and is shown to the shopper as formatted text: "
    "&lt;strong&gt;Color&lt;/strong&gt; reads as a bold Color on the storefront."
)


def _dealer_delta_prefetch():
    """Every dealer tier row for the whole page, in ONE query, already named.

    `DealerTierOptionPrice.tier_group` stores a `DealerGroup` CODE, so a column
    that showed the merchant's word for the group would be a lookup per row on a
    screen that renders every choice a question has. The name rides back on the
    prefetch as a correlated column instead, which costs nothing extra: it is
    one more column on a query that had to run anyway.
    """
    from ..dealer.models import DealerGroup

    return Prefetch(
        "tier_deltas",
        queryset=DealerTierOptionPrice.objects.annotate(
            wsm_group_name=Subquery(
                DealerGroup.objects.filter(code=OuterRef("tier_group")).values(
                    "name"
                )[:1]
            )
        ).order_by("tier_group"),
    )


class OptionValueInline(admin.TabularInline):
    model = OptionValue
    form = OptionValueInlineForm
    formset = OptionValueInlineFormSet
    extra = 3
    fields = (
        "sort_order",
        "name",
        "sku_fragment",
        "price_delta",
        "image_url",
        "dealer_prices",
    )
    readonly_fields = ("dealer_prices",)
    show_change_link = True

    def get_queryset(self, request):
        """One prefetch for the tier rows, one annotated column for the currency.

        The merchant walk of 2026-09-08 opened a question that carries 1,062
        dealer tier rows across its choices and the screen said nothing about
        any of them: a merchant editing a retail price could not see that the
        same choice was priced separately for two dealer groups. Django cannot
        nest an inline inside an inline and a dependency that fakes it is not
        worth the money, so this column REPORTS the tier rows and links to the
        one screen that edits them.

        A choice prints itself as "Black (Fuel Lab QSST: Colour)", so every row
        Django renders asks for its question and that question's product. That
        was two queries a row before this column existed; `select_related` pays
        for them once, and the query-count pin below holds it there.
        """
        rows = (
            super()
            .get_queryset(request)
            .select_related("option_set__product")
            .prefetch_related(_dealer_delta_prefetch())
        )
        return _with_currency(rows, "option_set__product_id")

    @admin.display(description="Dealer prices")
    def dealer_prices(self, obj):
        """Read from the prefetch. Never a query per row, whatever the row count.

        One group per line and no wrapping inside a line: rendered as running
        text the cell folded "Dealer 1 +6.65 USD, Dealer 2 +4.52 USD" into a
        nine line ribbon and made every row of the inline 200 pixels tall.
        The row's own "Change" link, above, is the way in to edit these.
        """
        if obj is None or obj.pk is None:
            # One of the blank rows the inline offers. It has no tier prices
            # because it is not a choice yet.
            return "-"
        rows = list(obj.tier_deltas.all())
        if not rows:
            return format_html(
                '<a href="{}">Add dealer prices</a>',
                reverse(
                    f"{self.admin_site.name}:wsm_compose_optionvalue_change",
                    args=[obj.pk],
                ),
            )
        currency = getattr(obj, "wsm_currency", "") or ""
        return format_html_join(
            mark_safe("<br>"),
            '<span style="white-space: nowrap">{} {}</span>',
            (
                (
                    row.wsm_group_name or row.tier_group,
                    money(row.price_delta, currency, signed=True),
                )
                for row in rows
            ),
        )

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        label_money_field(
            formset, "price_delta", obj.product_id if obj else None
        )
        return formset


class DealerTierOptionPriceInline(admin.TabularInline):
    model = DealerTierOptionPrice
    form = DealerTierOptionPriceForm
    formset = DealerTierOptionPriceFormSet
    extra = 1
    fields = ("tier_group", "price_delta")

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        label_money_field(
            formset, "price_delta", obj.option_set.product_id if obj else None
        )
        return formset


@admin.register(OptionSet, site=site)
class OptionSetAdmin(ProductFilteredMixin, WsmAdminMixin, admin.ModelAdmin):
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
    # The autocomplete widget pages its results, so the list it pages needs an
    # order. Without one Postgres is free to return a row twice across pages.
    ordering = ("product__name", "sort_order", "name")
    # A dropdown of every product is unusable past a few hundred SKUs, and the
    # fleet's smallest catalog is larger than that. `autocomplete_fields` is not
    # a dropdown: it is the AJAX search box that `ComposeProductPickerAdmin`
    # below already answers, so the merchant sees "Bushwacker Pocket Flare"
    # where `raw_id_fields` showed them the number 800001.
    autocomplete_fields = ("product",)
    inlines = [OptionValueInline]

    def get_queryset(self, request):
        # One annotated query, not one COUNT per row.
        return super().get_queryset(request).annotate(_values=Count("values"))

    def get_form(self, request, obj=None, **kwargs):
        """Say what the markup in the help field DOES, without touching the data.

        The stored text is not rewritten and not sanitised: a merchant who wrote
        HTML in 5.0 means it, and stripping it here would silently change what
        their shoppers read. `get_form` builds a fresh form class per request,
        so writing to `base_fields` is local to this page, the same reasoning
        `label_money_field` runs on.
        """
        form = super().get_form(request, obj, **kwargs)
        field = form.base_fields.get("note")
        if field is not None:
            field.help_text = f"{field.help_text} {NOTE_IS_HTML}".strip()
        return form

    @admin.display(description="Product", ordering="product__name")
    def product_name(self, obj):
        return format_html(
            '<a href="{}">{}</a>', self.product_url(obj.product), _cell(obj.product.name)
        )

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
        "price_change",
        "sort_order",
        "option_set",
    )
    list_select_related = ("option_set", "option_set__product")
    search_fields = ("name", "sku_fragment", "option_set__product__name")
    autocomplete_fields = ("option_set",)
    inlines = [DealerTierOptionPriceInline]

    def get_queryset(self, request):
        return _with_currency(super().get_queryset(request), "option_set__product_id")

    @admin.display(description="Product", ordering="option_set__product__name")
    def product(self, obj):
        return _cell(obj.option_set.product)

    @admin.display(description="Price change", ordering="price_delta")
    def price_change(self, obj):
        return money(
            obj.price_delta, getattr(obj, "wsm_currency", "") or "", signed=True
        )


@admin.register(Fee, site=site)
class FeeAdmin(ProductFilteredMixin, WsmAdminMixin, admin.ModelAdmin):
    """A charge, in a merchant's words. See FeeForm for the labels.

    The hidden variant is created by the first configured add, never by hand, so
    it is off the form and read-only in a collapsed Internal section that
    support can open.
    """

    form = FeeForm
    list_display = (
        "product_name",
        "label",
        "sku",
        "charged_as",
        "charge",
        "how_often",
        "required",
    )
    # The product cell is a link to the product, so it cannot also be the link
    # into the charge: Django nests one anchor inside the other and the merchant
    # loses the only doorway to the row.
    list_display_links = ("label",)
    list_select_related = ("product",)
    list_filter = ("basis", "apply_to", "required")
    search_fields = ("label", "sku", "product__name")
    autocomplete_fields = ("product",)
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

    def get_queryset(self, request):
        return _with_currency(super().get_queryset(request), "product_id")

    @admin.display(description="Product", ordering="product__name")
    def product_name(self, obj):
        return format_html(
            '<a href="{}">{}</a>', self.product_url(obj.product), _cell(obj.product.name)
        )

    @admin.display(description="Charged as", ordering="basis")
    def charged_as(self, obj):
        return obj.get_basis_display()

    @admin.display(description="Charge", ordering="amount")
    def charge(self, obj):
        """One column, two units: the basis decides which one this row is in."""
        if obj.basis == pricing.PERCENT:
            return f"{obj.amount}%"
        return money(obj.amount, getattr(obj, "wsm_currency", "") or "")

    @admin.display(description="How often", ordering="apply_to")
    def how_often(self, obj):
        return obj.get_apply_to_display()


# One product exists per fee, created by `Fee.ensure_variant` under this product
# type, only because an order line needs a variant to hang money on. The merchant
# walk found them sitting in the product lookup named after the charge, where
# picking one would hang a question off something that is not on the shelf.
FEE_CARRIER_PRODUCT_TYPE_SLUG = "wsm-fee"


@admin.register(Product, site=site)
class ComposeProductPickerAdmin(WsmAdminMixin, admin.ModelAdmin):
    """Read-only product list, so the option-set lookup popup resolves.

    Registered because `autocomplete_fields` needs a changelist to search,
    because products are edited here: Saleor's own Dashboard owns the catalog.
    Every write is refused regardless of what the user was granted.
    """

    list_display = ("name", "slug", "product_type", "option_sets", "fees")
    search_fields = ("name", "slug", "variants__sku")
    ordering = ("name",)

    def get_queryset(self, request):
        """Two counts, no extra round trip and no join that multiplies rows.

        A pair of `Count` aggregates over two reverse relations would cross-join
        them and count each set once per fee; correlated subqueries are two more
        columns on the query the changelist already runs.
        """
        return (
            super()
            .get_queryset(request)
            .exclude(product_type__slug=FEE_CARRIER_PRODUCT_TYPE_SLUG)
            .annotate(
                wsm_set_count=_related_count(OptionSet),
                wsm_fee_count=_related_count(Fee),
            )
        )

    def _linked_count(self, obj, model_name, count):
        # The subquery has no row to return where the product has none, so the
        # annotation is NULL rather than 0.
        if not count:
            return "-"
        url = reverse(
            f"{self.admin_site.name}:wsm_compose_{model_name}_changelist"
        )
        return format_html(
            '<a href="{}?product__id__exact={}">{}</a>', url, obj.pk, count
        )

    @admin.display(description="Option sets", ordering="wsm_set_count")
    def option_sets(self, obj):
        return self._linked_count(obj, "optionset", obj.wsm_set_count)

    @admin.display(description="Fees", ordering="wsm_fee_count")
    def fees(self, obj):
        return self._linked_count(obj, "fee", obj.wsm_fee_count)

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
