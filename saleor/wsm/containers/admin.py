# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The merchant's screens for series and kits.

These land on the SAME AdminSite the compose unit mounts at /admin/: one login,
one sidebar, one merchant. That site is what U2 adds to core's settings. The collection and the variant are raw id fields on
purpose: a select box that loads every collection in a real catalog is a screen
that never opens.
"""

from django.contrib import admin

from ..compose.admin import WsmAdminMixin
from ..compose.admin import site as merchant_site
from .models import KitConfig, KitMember, SeriesConfig


class SeriesConfigAdmin(WsmAdminMixin, admin.ModelAdmin):
    list_display = ("collection", "brand", "partitioning_axis", "published")
    list_filter = ("published",)
    search_fields = ("brand", "collection__slug", "collection__name")
    raw_id_fields = ("collection",)


class KitMemberInline(WsmAdminMixin, admin.TabularInline):
    model = KitMember
    extra = 3
    fields = ("variant", "quantity", "sort_order")
    raw_id_fields = ("variant",)


class KitConfigAdmin(WsmAdminMixin, admin.ModelAdmin):
    list_display = ("collection", "discount_kind", "discount_amount", "active")
    list_filter = ("active", "discount_kind")
    search_fields = ("collection__slug", "collection__name")
    raw_id_fields = ("collection",)
    inlines = [KitMemberInline]


def register(site):
    """Put both screens on a merchant AdminSite. One call, one place to change."""
    site.register(SeriesConfig, SeriesConfigAdmin)
    site.register(KitConfig, KitConfigAdmin)
    return site


register(merchant_site)
