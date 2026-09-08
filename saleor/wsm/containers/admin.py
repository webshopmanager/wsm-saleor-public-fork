# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The merchant's screens for series and kits.

These land on the SAME AdminSite the compose unit mounts at /admin/: one login,
one sidebar, one merchant. That site is what U2 adds to core's settings, so on a
containers-only branch there is nothing to register onto and `register()` is
called by a test instead. The collection and the variant are raw id fields on
purpose: a select box that loads every collection in a real catalog is a screen
that never opens.
"""

from django.contrib import admin

from .models import KitConfig, KitMember, SeriesConfig


class WsmAdminMixin:
    """Saleor's User has no `has_module_perms`, so the admin cannot ask for one.

    ponytail: the twin of the mixin in saleor/wsm/compose/admin.py, duplicated
    rather than imported because that file is not on this branch yet. The two
    collapse into one import the day both units sit on one branch.
    """

    def has_module_permission(self, request):
        user = request.user
        if not user.is_active or not user.is_staff:
            return False
        if user.is_superuser:
            return True
        return any(self.get_model_perms(request).values())


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


try:
    from ..compose.admin import site as _merchant_site
except ImportError:  # the compose unit has not landed on this branch yet
    pass
else:
    register(_merchant_site)
