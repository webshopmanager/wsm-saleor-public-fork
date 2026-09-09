# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Django admin, which is how a merchant sets a tier price (design doc section 6).

Every list here answers "find me the row I mean": the merchant walk of
2026-09-08 opened 626 tier prices that all read "Base" with no search box, and
a dealer-customer list of UUIDs. Saleor's `__str__` is the cause in both cases,
so every column that names a core object is an explicit display method and
every foreign key is an autocomplete over the pickers in
`saleor/wsm/admin_pickers.py`.

These land on the SAME AdminSite the compose unit mounts at /admin/: one login,
one sidebar, one merchant. Django's default `admin.site` is not mounted by this
fork and collects a ModelAdmin from every installed app, which is why compose
runs a private one. `WsmAdminMixin` comes from there too: Saleor's User has no
`has_module_perms`, so a non-superuser merchant 500s on the index without it.
"""

from django import forms
from django.contrib import admin, messages
from django.db.models import OuterRef, Subquery
from django.shortcuts import redirect
from django.urls import reverse

from ..admin_pickers import PickerLabelMixin
from ..compose.admin import WsmAdminMixin
from ..compose.admin import site as merchant_site
from ..compose.forms import CENT, currency_for, money
from .models import DealerCustomer, DealerGroup, DealerSettings, TierPrice


@admin.register(DealerGroup, site=merchant_site)
class DealerGroupAdmin(WsmAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name")
    search_fields = ("code", "name")


@admin.register(DealerCustomer, site=merchant_site)
class DealerCustomerAdmin(PickerLabelMixin, WsmAdminMixin, admin.ModelAdmin):
    list_display = ("email", "full_name", "group", "tax_exempt")
    list_filter = ("group", "tax_exempt")
    list_select_related = ("user", "group")
    autocomplete_fields = ("user",)
    search_fields = ("user__email", "user__first_name", "user__last_name")
    ordering = ("user__email",)

    @admin.display(description="Email", ordering="user__email")
    def email(self, obj):
        return obj.user.email

    @admin.display(description="Name", ordering="user__last_name")
    def full_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or "-"


class TierPriceAdminForm(forms.ModelForm):
    """Two decimal places on the way in, and the currency named on the label.

    The column stores three places to match `CheckoutLine.price_override`, which
    is right for storage and wrong for a merchant: nobody means a tenth of a
    cent. The form therefore takes two. A row that already holds a third decimal
    is shown exactly as stored rather than rounded for the screen, because
    hiding a number that is really there is the worse of the two lies; the form
    then refuses the save until the merchant fixes it.
    """

    amount = forms.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        model = TierPrice
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["amount"]
        field.help_text = TierPrice._meta.get_field("amount").help_text
        product_id = (
            self.instance.variant.product_id if self.instance.variant_id else None
        )
        currency = currency_for(product_id)
        field.label = "What this group pays each"
        if currency:
            field.label = f"{field.label} ({currency})"
        stored = self.instance.amount if self.instance.pk else None
        if stored is not None and stored == stored.quantize(CENT):
            self.initial["amount"] = stored.quantize(CENT)


@admin.register(TierPrice, site=merchant_site)
class TierPriceAdmin(PickerLabelMixin, WsmAdminMixin, admin.ModelAdmin):
    """The screen a merchant lives on. One row per SKU, group and quantity break."""

    form = TierPriceAdminForm
    list_display = ("product_name", "sku", "group", "min_quantity", "amount_display")
    list_filter = ("group",)
    # Three joins the changelist would otherwise make once per row, on a page
    # of 100 rows: the product name and the SKU are read for every line.
    list_select_related = ("variant", "variant__product", "group")
    autocomplete_fields = ("variant",)
    search_fields = ("variant__product__name", "variant__sku")
    ordering = ("variant__product__name", "variant__sku", "group__code", "min_quantity")

    @admin.display(description="Product", ordering="variant__product__name")
    def product_name(self, obj):
        return obj.variant.product.name

    @admin.display(description="SKU", ordering="variant__sku")
    def sku(self, obj):
        return obj.variant.sku or "(no SKU)"

    def get_queryset(self, request):
        """The currency comes back with the row, not one query per row.

        626 rows at 100 to a page is 100 extra round trips if the column asks
        per row. A correlated subquery is a column on the query already being
        run, so the changelist still costs one.
        """
        from ...product.models import ProductChannelListing

        return (
            super()
            .get_queryset(request)
            .annotate(
                wsm_currency=Subquery(
                    ProductChannelListing.objects.filter(
                        product_id=OuterRef("variant__product_id")
                    ).values("currency")[:1]
                )
            )
        )

    @admin.display(description="Amount", ordering="amount")
    def amount_display(self, obj):
        return money(obj.amount, getattr(obj, "wsm_currency", "") or "")


@admin.register(DealerSettings, site=merchant_site)
class DealerSettingsAdmin(WsmAdminMixin, admin.ModelAdmin):
    """A singleton, so the list is a doorway rather than a screen.

    The model treats "no row" as the defaults, which is right for a fresh
    install and wrong for a merchant: the walk found an empty list with nothing
    on it saying that stacking is off. The changelist now creates the row with
    its defaults and opens it, so the toggle a merchant is looking for is always
    a page with the answer on it.
    """

    list_display = ("__str__", "discount_stacking")

    def has_add_permission(self, request):
        # One row or none: a second would make "the toggle" ambiguous.
        return super().has_add_permission(request) and not DealerSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Deleting the row would silently revert to the defaults, which is the
        # empty screen this admin exists to remove.
        return False

    def changelist_view(self, request, extra_context=None):
        row = DealerSettings.objects.first()
        if row is None:
            if not self.has_add_permission(request):
                self.message_user(
                    request,
                    "No dealer settings row exists yet, so the defaults apply: "
                    "discount stacking is OFF.",
                    messages.INFO,
                )
                return super().changelist_view(request, extra_context)
            row = DealerSettings.objects.create()
        return redirect(
            reverse(
                f"{self.admin_site.name}:wsm_dealer_dealersettings_change",
                args=[row.pk],
            )
        )
