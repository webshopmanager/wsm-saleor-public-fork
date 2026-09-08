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

from django.contrib import admin

from ...product.models import Product
from .models import DealerTierOptionPrice, Fee, OptionSet, OptionValue


class ComposeAdminSite(admin.AdminSite):
    site_header = "WSM Compose"
    site_title = "WSM Compose"
    index_title = "Product configuration"


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
    extra = 3
    fields = ("name", "sku_fragment", "price_delta", "image_url", "sort_order")
    show_change_link = True


class DealerTierOptionPriceInline(admin.TabularInline):
    model = DealerTierOptionPrice
    extra = 1
    fields = ("tier_group", "price_delta")


@admin.register(OptionSet, site=site)
class OptionSetAdmin(WsmAdminMixin, admin.ModelAdmin):
    list_display = ("name", "label", "product", "prompt_type", "required", "sort_order")
    list_filter = ("prompt_type", "required")
    search_fields = ("name", "label")
    # A dropdown of every product is unusable past a few hundred SKUs, and the
    # fleet's smallest catalog is larger than that. The lookup popup below is
    # what makes this screen survive a real catalog.
    raw_id_fields = ("product",)
    inlines = [OptionValueInline]


@admin.register(OptionValue, site=site)
class OptionValueAdmin(WsmAdminMixin, admin.ModelAdmin):
    """Where a dealer tier row is added: the tier price hangs off the VALUE.

    Django cannot nest an inline inside an inline, so the tier rows live one
    click deeper, on the value they price.
    """

    list_display = ("name", "option_set", "sku_fragment", "price_delta", "sort_order")
    list_filter = ("option_set__product",)
    search_fields = ("name", "sku_fragment")
    raw_id_fields = ("option_set",)
    inlines = [DealerTierOptionPriceInline]


@admin.register(Fee, site=site)
class FeeAdmin(WsmAdminMixin, admin.ModelAdmin):
    list_display = ("label", "product", "sku", "basis", "amount", "apply_to", "required")
    list_filter = ("basis", "apply_to", "required")
    search_fields = ("label", "sku")
    raw_id_fields = ("product",)
    # The hidden variant is created by the first configured add, never by hand.
    readonly_fields = ("variant",)


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
