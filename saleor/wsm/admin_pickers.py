# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Lookup screens for the core objects the fork's own tables point at.

Every foreign key from a fork table into Saleor (a user, a variant, a
collection) needs a way to FIND the row. Django resolves both the raw-id popup
and the autocomplete widget against the TARGET model's ModelAdmin on the same
AdminSite, so with no ModelAdmin for User, ProductVariant or Collection the
field degrades to a bare numeric id box with nothing behind it. The merchant
walk of 2026-09-08 rated every one of those fields unusable: one defect in four
places, fixed here once for every screen that points at core.

Two labelling problems come with it, and they are the same defect twice.
Saleor's `User.__str__` is a UUID, and `ProductVariant.__str__` is the variant
name, which is the string "Base" on every single-variant product: the tier-price
list showed 626 rows of "Base". `PickerAdmin.picker_label` is the ONE place that
says how a core object is named to a merchant. `LabelledAutocompleteJsonView`
uses it for the rows the widget offers, `PickerLabelMixin` for the row already
chosen, and `list_display` here for the popup's own table.

These are pickers, not editors: Saleor's own Dashboard owns users, variants and
collections. Every write is refused whatever the user was granted, and
`has_module_permission` is False so they never appear on the admin index beside
the things a merchant does edit.
"""

from types import MethodType
from typing import TYPE_CHECKING

from django.contrib import admin
from django.contrib.admin.exceptions import NotRegistered
from django.contrib.admin.options import BaseModelAdmin
from django.contrib.admin.views.autocomplete import AutocompleteJsonView

from ..account.models import User
from ..product.models import Collection, ProductVariant
from .compose.admin import SkuRankedSearchMixin
from .compose.admin import site as merchant_site

# Who may look a core object up. A picker is opened from the screen that points
# at it, so the answer is "whoever may work on that screen": listing the
# permissions explicitly keeps the lookup no wider than the thing it serves.
USER_LOOKUP_PERMISSIONS = (
    "wsm_dealer.view_dealercustomer",
    "wsm_dealer.add_dealercustomer",
    "wsm_dealer.change_dealercustomer",
)
VARIANT_LOOKUP_PERMISSIONS = (
    "wsm_dealer.view_tierprice",
    "wsm_dealer.add_tierprice",
    "wsm_dealer.change_tierprice",
    "wsm_containers.view_kitmember",
    "wsm_containers.add_kitmember",
    "wsm_containers.change_kitmember",
)
COLLECTION_LOOKUP_PERMISSIONS = (
    "wsm_containers.view_seriesconfig",
    "wsm_containers.add_seriesconfig",
    "wsm_containers.change_seriesconfig",
    "wsm_containers.view_kitconfig",
    "wsm_containers.add_kitconfig",
    "wsm_containers.change_kitconfig",
)


class PickerAdmin(admin.ModelAdmin):
    """A read-only changelist that exists so a lookup has somewhere to point.

    Subclasses set `lookup_permissions` and `picker_label`; nothing else about
    them may write, because the row belongs to Saleor's Dashboard.
    """

    lookup_permissions: tuple[str, ...] = ()

    def picker_label(self, obj):
        """How this object is named wherever a merchant picks it."""
        return str(obj)

    def has_module_permission(self, request):
        # Off the index: a merchant has no business "managing users" here, and
        # the lookup views do not consult module permission.
        return False

    def has_view_permission(self, request, obj=None):
        user = request.user
        if not user.is_active or not user.is_staff:
            return False
        if user.is_superuser:
            return True
        return any(user.has_perm(codename) for codename in self.lookup_permissions)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# Mixed with both ModelAdmin and TabularInline subclasses below, so the
# fake base for mypy is their real common ancestor. Runtime base stays
# `object`.
if TYPE_CHECKING:
    _BaseModelAdminBase = BaseModelAdmin
else:
    _BaseModelAdminBase = object


class PickerLabelMixin(_BaseModelAdminBase):
    """Name core objects the merchant's way in every FK widget on this screen.

    The widget renders the ALREADY CHOSEN row from `label_from_instance`, which
    defaults to `str(obj)`: without this, a saved tier price reopens showing
    "Base" even though the search that found it showed the product and the SKU.
    """

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if formfield is None:
            return formfield
        try:
            target = self.admin_site.get_model_admin(db_field.remote_field.model)
        except NotRegistered:
            return formfield
        if isinstance(target, PickerAdmin):
            # Documented Django hook; the stub types it as a plain method.
            formfield.label_from_instance = target.picker_label  # type: ignore[method-assign]
        return formfield


class LabelledAutocompleteJsonView(AutocompleteJsonView):
    """The stock view, with the picker's label instead of `str(obj)`.

    `process_request` has already resolved `self.model_admin` to the TARGET
    admin by the time results are serialized, so this asks the picker itself.
    """

    def serialize_result(self, obj, to_field_name):
        # django-stubs' AutocompleteJsonView stub has not caught up with
        # this Django 5.2 method; it exists at runtime.
        result = super().serialize_result(obj, to_field_name)  # type: ignore[misc]
        label = getattr(self.model_admin, "picker_label", None)
        if label is not None:
            result["text"] = label(obj)
        return result


def _labelled_autocomplete_view(self, request):
    return LabelledAutocompleteJsonView.as_view(admin_site=self)(request)


def install_labelled_autocomplete(site):
    """Point the site's ONE autocomplete endpoint at the labelled view.

    Django 4.0 moved autocomplete off ModelAdmin onto the AdminSite, so a single
    view answers every autocomplete field on the site and no ModelAdmin can
    override it. The site object belongs to wsm.compose, so this rebinds one
    method on it rather than editing that file, in the same shape as the fork's
    other installs. It runs at admin autodiscovery; `AdminSite.get_urls` reads
    `self.autocomplete_view` when the URLconf first loads, which is later.
    """
    site.autocomplete_view = MethodType(_labelled_autocomplete_view, site)
    return site


@admin.register(User, site=merchant_site)
class UserPickerAdmin(PickerAdmin):
    """Find a shopper by email. `User.__str__` is a UUID, so nothing here uses it."""

    lookup_permissions = USER_LOOKUP_PERMISSIONS
    list_display = ("email", "full_name", "is_active")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("email",)

    @admin.display(description="Name", ordering="last_name")
    def full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or "-"

    def picker_label(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return f"{obj.email} ({name})" if name else obj.email


@admin.register(ProductVariant, site=merchant_site)
class ProductVariantPickerAdmin(SkuRankedSearchMixin, PickerAdmin):
    """Find a SKU. The product's name carries the meaning; the variant name is "Base"."""

    lookup_permissions = VARIANT_LOOKUP_PERMISSIONS
    # This model IS the variant, so the SKU is on the row itself, and the name
    # a merchant reads is the product's.
    sku_owner_field = "pk"
    name_field = "product__name"
    list_display = ("product_name", "sku", "name")
    search_fields = ("sku", "product__name", "name")
    list_select_related = ("product",)
    ordering = ("product__name", "sku")

    def get_queryset(self, request):
        # The label reads the product on every row, here and in the widget.
        return super().get_queryset(request).select_related("product")

    @admin.display(description="Product", ordering="product__name")
    def product_name(self, obj):
        return obj.product.name

    def picker_label(self, obj):
        return f"{obj.product.name} - {obj.name or 'default'} [{obj.sku or 'no SKU'}]"


@admin.register(Collection, site=merchant_site)
class CollectionPickerAdmin(PickerAdmin):
    """Find the collection a series or a kit is built on."""

    lookup_permissions = COLLECTION_LOOKUP_PERMISSIONS
    list_display = ("name", "slug")
    search_fields = ("name", "slug")
    ordering = ("name",)

    def picker_label(self, obj):
        return f"{obj.name} ({obj.slug})"


install_labelled_autocomplete(merchant_site)
