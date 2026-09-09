# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The merchant's forms: the model's rules, said in the merchant's words.

Nothing here decides anything. Every refusal comes from a model `clean()` in
models.py, so a writer that never renders a form gets the same answer. What
lives here is the part a model cannot know: the wording on a label, the currency
a channel prices in, and the two rules that span a whole inline formset instead
of one row.

Why the formset owns the floor while the option-set screen is open: a merchant
editing a question edits several credits in one submit, and a row checked
against its STORED siblings would refuse the very submit that fixes them. The
rows carry `floor_checked_by_formset` so the model skips its own single-row
version exactly there, and nowhere else.
"""

from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet

from .models import (
    Fee,
    OptionSet,
    OptionValue,
    configured_floor_cents,
    dealer_floor_problem,
    duplicate_fragment_error,
    floor_error,
    tier_group_choices,
)

# Short on purpose: these are the column headers of a tabular inline, and a
# header that wraps is what made the option-set list unreadable in the first
# place. The help text under each field carries the detail.
VALUE_LABELS = {
    "name": "Choice",
    "sku_fragment": "SKU code",
    "price_delta": "Price change",
    "image_url": "Image",
}


def currency_for(product_id) -> str:
    """The currency this product is priced in, for the amount label.

    A merchant typing into a box labelled "Amount" has to guess. The product's
    own channel listing is the only honest answer, and the shop's channel is the
    fallback while a charge is being added and no product is chosen yet.
    """
    from ...channel.models import Channel
    from ...product.models import ProductChannelListing

    if product_id:
        currency = (
            ProductChannelListing.objects.filter(product_id=product_id)
            .values_list("currency", flat=True)
            .first()
        )
        if currency:
            return currency
    return Channel.objects.values_list("currency_code", flat=True).first() or ""


CENT = Decimal("0.01")


def money(amount, currency: str = "", signed: bool = False) -> str:
    """An amount the way a merchant reads it: two places, and the currency.

    Storage precision is not screen precision. `TierPrice.amount` keeps three
    decimal places so it round-trips onto `CheckoutLine.price_override` without
    a quantize that could move a cent, and a merchant scanning 626 rows reads
    "228.000" as a bug and "0.000" as free. Nothing formatted here is ever
    saved; the stored number is untouched.

    `signed` is for a price CHANGE, where the plus is the whole meaning: an
    option credit and an option surcharge are otherwise the same string.
    """
    if amount is None:
        return "-"
    shown = Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)
    text = f"+{shown}" if signed and shown > 0 else f"{shown}"
    return f"{text} {currency}".strip()


def label_money_field(formset, field_name: str, product_id) -> None:
    """Name the currency on an inline's money column, once per page.

    The alternative is asking per row, which is one query per rendered form on a
    screen that renders every existing row plus the extras. `get_formset` builds
    a fresh form class per call, so writing to `base_fields` here is local to
    this page and not a process-wide mutation of the declared form.
    """
    field = formset.form.base_fields.get(field_name)
    if field is None:
        return
    currency = currency_for(product_id)
    if currency:
        field.label = f"{field.label} ({currency})"


class FeeForm(forms.ModelForm):
    """Defect 4: developer vocabulary, no currency, no percent semantics.

    `variant` is off the form entirely. It is written by the first configured
    add, never by hand, and a merchant editing it can only break the charge. The
    admin still SHOWS it, read-only, in a collapsed Internal section, because
    support needs to know which hidden row a charge rides on.
    """

    class Meta:
        model = Fee
        exclude = ("variant",)
        labels = {
            "product": "Product",
            "label": "Charge name shown to the shopper",
            "sku": "Your code for this charge",
            "basis": "Charged as",
            "apply_to": "How often",
            "required": "Always charged",
            "decline_label": "Wording when the shopper declines",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        currency = currency_for(
            self.instance.product_id or self.initial.get("product")
        )
        if currency:
            self.fields["amount"].label = f"Amount ({currency}, or a percentage)"


class DealerTierOptionPriceForm(forms.ModelForm):
    """Defect 3: free text saved a group nobody belongs to and reported success.

    A dropdown of the groups that exist, so the no-op cannot be typed. The model
    still validates the code (`DealerTierOptionPrice.clean`), because a dropdown
    is a courtesy and not a rule: an importer or a shell writes straight past it.
    """

    class Meta:
        fields = "__all__"
        labels = {"price_delta": "This group pays"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        codes = tier_group_choices()
        current = self.instance.tier_group if self.instance.pk else ""
        # A row pointing at a group that has since been deleted still has to
        # render. It fails validation on save, which is the right place to hear
        # about it.
        if current and current not in codes:
            codes = [current] + codes
        self.fields["tier_group"] = forms.ChoiceField(
            label="Dealer group",
            choices=[("", "---------")] + [(code, code) for code in codes],
            help_text=(
                "Dealer groups are managed under Dealer groups. A price for a "
                "group that does not exist is never charged to anyone."
            ),
        )


class DealerTierOptionPriceFormSet(BaseInlineFormSet):
    """Django's own duplicate message names the COLUMN, which is our word.

    "Please correct the duplicate data for tier_group." is what a merchant saw
    for pricing the same group twice on one choice. The rule is worth keeping;
    only the wording was ours to fix.
    """

    def get_unique_error_message(self, unique_check):
        # The check arrives as ("option_value", "tier_group") from the model
        # constraint and as ("tier_group",) once the parent key is excluded from
        # the inline form. Both are this rule.
        if "tier_group" in unique_check:
            return ValidationError(
                "This choice already has a price for that dealer group. Change "
                "the group, or edit the row that already has it."
            )
        return super().get_unique_error_message(unique_check)


class OptionSetAdminForm(forms.ModelForm):
    """Required and the prompt type both move the floor, and so do the values."""

    class Meta:
        model = OptionSet
        fields = "__all__"
        labels = {
            "label": "Question shown to the shopper",
            "name": "Internal name",
            "prompt_type": "How the shopper answers",
            "note": "Help shown under the question",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.floor_checked_by_formset = True


class OptionValueAdminForm(forms.ModelForm):
    """The choice on its own page, where the model checks it row at a time.

    No `floor_checked_by_formset` here: one row submitted alone IS the whole
    submit, so the model's own check is the right one.
    """

    class Meta:
        model = OptionValue
        fields = "__all__"
        labels = VALUE_LABELS


class OptionValueInlineForm(forms.ModelForm):
    class Meta:
        model = OptionValue
        fields = "__all__"
        labels = VALUE_LABELS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.floor_checked_by_formset = True


class OptionValueInlineFormSet(BaseInlineFormSet):
    """The two rules a single row cannot see, checked across the whole submit.

    Three new credits added at once are each fine against the database and
    broken together; two new choices given the same SKU code are each unique
    against the database and identical to each other. Both were reachable from
    the screen the merchant walk used. The formset holds every value the
    question has, so pending against pending is the whole picture.
    """

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        pending, removed = [], []
        for form in self.forms:
            if not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                if form.instance.pk:
                    removed.append(form.instance.pk)
                continue
            pending.append(form)

        seen = {}
        for form in pending:
            fragment = form.cleaned_data.get("sku_fragment")
            if not fragment:
                continue
            if fragment in seen:
                form.add_error(
                    "sku_fragment", duplicate_fragment_error(seen[fragment], fragment)
                )
            else:
                seen[fragment] = form.cleaned_data.get("name") or "another choice"
        if any(self.errors):
            return

        product_id = getattr(self.instance, "product_id", None)
        if not product_id:
            return
        values = []
        for form in pending:
            form.instance.option_set_id = self.instance.pk
            values.append(form.instance)
        # The edit as it WOULD be saved, asked once of retail and once of every
        # dealer group, because both floors read the same pending rows.
        edit = {
            "pending_set": self.instance,
            "pending_values": values,
            "removed_value_pks": removed,
        }
        floor, base = configured_floor_cents(product_id, **edit)
        if floor is not None and floor <= 0:
            raise ValidationError(floor_error(floor, base))
        # And once per dealer group with rows on this product. The retail floor
        # is only an upper bound on a dealer's: a group whose credits are deeper
        # goes under first, and the same submit is the merchant's last chance to
        # hear about it before that group's add-to-cart starts refusing.
        problem = dealer_floor_problem(product_id, **edit)
        if problem is not None:
            raise problem
