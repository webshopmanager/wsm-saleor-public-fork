# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Django admin, which is how a merchant sets a tier price (design doc section 6).

`raw_id_fields` on the two FKs into core: a select widget on User or
ProductVariant renders every row in the table, and a catalog of any size makes
the page unopenable.

These land on the SAME AdminSite the compose unit mounts at /admin/: one login,
one sidebar, one merchant. Django's default `admin.site` is not mounted by this
fork and collects a ModelAdmin from every installed app, which is why compose
runs a private one. `WsmAdminMixin` comes from there too: Saleor's User has no
`has_module_perms`, so a non-superuser merchant 500s on the index without it.
"""

from django.contrib import admin

from ..compose.admin import WsmAdminMixin
from ..compose.admin import site as merchant_site
from .models import DealerCustomer, DealerGroup, DealerSettings, TierPrice


@admin.register(DealerGroup, site=merchant_site)
class DealerGroupAdmin(WsmAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name")
    search_fields = ("code", "name")


@admin.register(DealerCustomer, site=merchant_site)
class DealerCustomerAdmin(WsmAdminMixin, admin.ModelAdmin):
    list_display = ("user", "group", "tax_exempt")
    list_filter = ("group", "tax_exempt")
    raw_id_fields = ("user",)
    search_fields = ("user__email",)


@admin.register(TierPrice, site=merchant_site)
class TierPriceAdmin(WsmAdminMixin, admin.ModelAdmin):
    list_display = ("variant", "group", "min_quantity", "amount")
    list_filter = ("group",)
    raw_id_fields = ("variant",)


@admin.register(DealerSettings, site=merchant_site)
class DealerSettingsAdmin(WsmAdminMixin, admin.ModelAdmin):
    list_display = ("__str__", "discount_stacking")

    def has_add_permission(self, request):
        # One row or none: a second would make "the toggle" ambiguous.
        return not DealerSettings.objects.exists()
