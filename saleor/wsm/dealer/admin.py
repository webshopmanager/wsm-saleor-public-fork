# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Django admin, which is how a merchant sets a tier price (design doc section 6).

`raw_id_fields` on the two FKs into core: a select widget on User or
ProductVariant renders every row in the table, and a catalog of any size makes
the page unopenable.
"""

from django.contrib import admin

from .models import DealerCustomer, DealerGroup, DealerSettings, TierPrice


@admin.register(DealerGroup)
class DealerGroupAdmin(admin.ModelAdmin):
    list_display = ("code", "name")
    search_fields = ("code", "name")


@admin.register(DealerCustomer)
class DealerCustomerAdmin(admin.ModelAdmin):
    list_display = ("user", "group", "tax_exempt")
    list_filter = ("group", "tax_exempt")
    raw_id_fields = ("user",)
    search_fields = ("user__email",)


@admin.register(TierPrice)
class TierPriceAdmin(admin.ModelAdmin):
    list_display = ("variant", "group", "min_quantity", "amount")
    list_filter = ("group",)
    raw_id_fields = ("variant",)


@admin.register(DealerSettings)
class DealerSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "discount_stacking")

    def has_add_permission(self, request):
        # One row or none: a second would make "the toggle" ambiguous.
        return not DealerSettings.objects.exists()
