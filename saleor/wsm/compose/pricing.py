# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Configured-line pricing: no database, no HTTP, no tenant vocabulary.

A port of the SEMANTICS of `internal/compose/pricing` in partslogic-search, not
of its code. Given a product's authoritative option sets and fees plus a
shopper's selections, this either refuses (fail closed) or returns the one line
to hand Saleor.

Money is integer cents throughout. Deltas are SIGNED, and that is the whole
pricing model: a credit value subtracts (requirements 1.1, 1.2). There is no
replacement price and no mode enum. "At or below zero refuses" holds on the unit
and on the line (1.3): money that came to nothing is a data bug, and clamping
would hide it.

Dealer tiers follow requirement 2.2 as corrected: a tier row is a component of
the configured price and not a discount on it, so a credit stands as written.
Two bounds hold around it. The ceiling is the retail delta floored at zero, and
a row above it is refused, so a tier row can never turn a free or credited value
into a surcharge a retail shopper does not pay. Under the ceiling the charge is
the BETTER OF the tier delta and the retail one, so a shallower dealer credit
cannot quote a dealer a higher price than retail for the same configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SNAPSHOT_VERSION = "compose.v1"
DEFAULT_SKU_SEPARATOR = "-"

# Fee basis. FIXED reads `amount` as cents; PERCENT reads it as hundredths of a
# percent, so 825 is 8.25%.
FIXED = "fixed"
PERCENT = "percent"
FEE_BASES = (FIXED, PERCENT)

# Fee scope. PER_UNIT charges once per unit ordered (5.0's only shape);
# PER_LINE charges once for the line whatever the quantity.
PER_UNIT = "unit"
PER_LINE = "line"
FEE_SCOPES = (PER_UNIT, PER_LINE)

CHOICE_ONE = "choice_one"
CHOICE_MANY = "choice_many"
# The prompt types that collect a payload instead of values. A required IMAGE
# axis fails closed here; the old 5.0 client renderer skipped image axes in its
# required check, which was a fail-open on a required axis.
TEXT_PROMPTS = ("text", "date", "datetime", "image")
PROMPT_TYPES = (CHOICE_ONE, CHOICE_MANY) + TEXT_PROMPTS


class ComposeRefusal(Exception):
    """Base for every refusal. Nothing here is ever clamped or ignored."""


def format_money(cents: int) -> str:
    """Integer cents as a signed, thousand-separated amount: -935000 -> -9,350.00.

    No currency symbol and no currency code: this module prices in cents and
    knows nothing about a channel, and threading a currency through every call
    to decorate one refusal would buy a symbol at the cost of a parameter on the
    pricing path. The storefront and the admin each know their own currency.
    """
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{sign}{whole:,}.{frac:02d}"


class NegativeTotalError(ComposeRefusal):
    """The selections were all legal and the catalog still produced no money.

    The message reaches a SHOPPER, in the 422 the storefront shows, so it is
    written in currency units and in words a shopper can act on. Cents read as a
    developer note ("configured price is -935000 cents"). The number is still on
    `total_cents` for anything that wants it.

    Reaching this at all now means the rows were written outside the merchant
    screens: `minimum_configured_cents` below is what `OptionValue.clean()`
    checks, so a catalog that can produce this cannot be saved in the admin.
    """

    def __init__(self, total_cents: int, *, line: bool = False):
        self.total_cents = total_cents
        self.line = line
        what = (
            "This item and its charges come to"
            if line
            else "This configuration comes to"
        )
        super().__init__(
            f"{what} {format_money(total_cents)}, which is not a valid price. "
            "Please contact us."
        )


class AboveRetailError(ComposeRefusal):
    """A tier row that charges MORE than the positive retail delta it replaces.

    Refused rather than clamped to retail: clamping would quietly price that
    dealer at retail forever and nobody would ever find the row.
    """

    def __init__(self, *, value_id: int, name: str, tier_group: str, amount: int, retail: int):
        self.value_id = value_id
        self.tier_group = tier_group
        super().__init__(
            f"option value {name!r} (id {value_id}) has a {tier_group!r} tier delta of "
            f"{amount} cents above its retail {retail} cents; refusing to price the line"
        )


class MissingRequiredError(ComposeRefusal):
    """A required option set was not answered."""


class UnknownValueError(ComposeRefusal):
    """A selection, or an accepted fee, names something this product does not carry."""


@dataclass(frozen=True)
class TierDelta:
    """One buyer group's delta for one option value. Resolved server-side."""

    tier_group: str
    price_delta: int


@dataclass(frozen=True)
class Value:
    id: int
    name: str = ""
    sku_fragment: str = ""
    price_delta: int = 0
    sort_order: int = 0
    tier_deltas: tuple[TierDelta, ...] = ()


@dataclass(frozen=True)
class OptionSet:
    id: int
    name: str = ""
    label: str = ""
    prompt_type: str = CHOICE_ONE
    required: bool = False
    note: str = ""
    sort_order: int = 0
    values: tuple[Value, ...] = ()

    @property
    def display_name(self) -> str:
        return self.label or self.name


@dataclass(frozen=True)
class Fee:
    id: int
    label: str = ""
    sku: str = ""
    basis: str = FIXED
    amount: int = 0
    apply_to: str = PER_UNIT
    # required False makes the fee DECLINABLE: charged only when the caller names
    # it in accepted_fee_ids. Default-deny, so a fee the shopper was never shown
    # cannot be charged by a caller that forgot to ask.
    required: bool = True
    decline_label: str = ""


@dataclass(frozen=True)
class Selection:
    set_id: int
    value_ids: tuple[int, ...] = ()
    text: str = ""


@dataclass(frozen=True)
class ConfiguredPrice:
    unit_cents: int
    composite_sku: str
    fee_total_cents: int
    snapshot: dict = field(default_factory=dict)


def delta_for(value: Value, tier_group: str | None) -> tuple[int, bool]:
    """The delta this buyer pays for one value, and whether a tier row supplied it.

    Public because the PDP endpoint shows the same number it will charge, and a
    second copy of this rule in a view is how a quoted price and a charged price
    drift apart.

    Requirement 2.2 as corrected: the ceiling is the retail delta floored at
    zero, and a tier row above it is refused rather than clamped, because a
    dealer surcharge on a value retail gives away is a data bug nobody would
    ever find if it silently priced at retail.

    Under that ceiling the answer is the BETTER OF the two deltas, which is the
    same rule a kit member already prices by ("dealer on a kit is better of,
    never both"). A tier row that is a SMALLER credit than retail is legal, is
    not a surcharge, and used to stand verbatim: the merchant walk measured a
    dealer quoted 3,698.99 for the configuration retail buys at 3,553.99, off a
    -300.00 tier credit sitting where retail gives -445.00. A dealer never pays
    more than retail for the same choice.
    """
    if not tier_group:
        return value.price_delta, False
    for row in value.tier_deltas:
        if row.tier_group != tier_group:
            continue
        if row.price_delta > max(value.price_delta, 0):
            raise AboveRetailError(
                value_id=value.id,
                name=value.name,
                tier_group=tier_group,
                amount=row.price_delta,
                retail=value.price_delta,
            )
        charged = min(row.price_delta, value.price_delta)
        # `tier_applied` records whether the tier was HONORED, not whether a row
        # was found: a row that lost to retail leaves the buyer at retail on this
        # value, and a line stamped as dealer-priced when it is not is how a
        # promotion gets suppressed on a retail price.
        return charged, charged == row.price_delta
    return value.price_delta, False


def _percent_of(base_cents: int, rate: int) -> int:
    """rate hundredths-of-a-percent of base, rounded HALF-UP away from zero.

    Integer arithmetic throughout: base and rate are both hundredths, so their
    product is scaled by 10,000 and one division carries the rounding. A float
    here lands a cent off on some inputs and nowhere else, the worst kind of
    money bug to find.
    """
    n = base_cents * rate
    neg = n < 0
    if neg:
        n = -n
    q = (n + 5000) // 10000
    return -q if neg else q


def _validate(sets_by_id, picked, fees_by_id, accepted):
    for set_id in picked:
        if set_id not in sets_by_id:
            raise UnknownValueError(f"unknown option set {set_id}")

    for option_set in sets_by_id.values():
        selection = picked.get(option_set.id)
        if selection is None:
            if option_set.required:
                raise MissingRequiredError(
                    f"required option {option_set.display_name!r} not selected"
                )
            continue
        if option_set.prompt_type in TEXT_PROMPTS:
            if selection.value_ids:
                raise UnknownValueError(
                    f"option {option_set.display_name!r} takes text, not values"
                )
            if option_set.required and not selection.text.strip():
                raise MissingRequiredError(
                    f"required option {option_set.display_name!r} is empty"
                )
            continue
        known = {v.id for v in option_set.values}
        for value_id in selection.value_ids:
            if value_id not in known:
                raise UnknownValueError(
                    f"unknown value {value_id} for option {option_set.display_name!r}"
                )
        if option_set.prompt_type == CHOICE_ONE and len(selection.value_ids) != 1:
            raise ComposeRefusal(
                f"option {option_set.display_name!r} takes exactly one value"
            )
        if option_set.prompt_type == CHOICE_MANY and not selection.value_ids:
            raise MissingRequiredError(
                f"option {option_set.display_name!r} needs at least one value"
            )
        if option_set.prompt_type not in PROMPT_TYPES:
            # An unrecognized prompt type is a data bug, and guessing how to
            # price it is worse than refusing.
            raise ComposeRefusal(
                f"option {option_set.display_name!r} has unknown prompt type "
                f"{option_set.prompt_type!r}"
            )

    # Accepting a fee this product does not carry, or one that was never
    # declinable, is request tampering: refused rather than ignored so a
    # storefront bug shows up loudly instead of as a silent undercharge.
    for fee_id in accepted:
        fee = fees_by_id.get(fee_id)
        if fee is None:
            raise UnknownValueError(f"unknown fee {fee_id} for this product")
        if fee.required:
            raise ComposeRefusal(
                f"fee {fee.label!r} is required and cannot be accepted or declined"
            )


def _apply_fees(fees, accepted, subtotal_cents: int, quantity: int):
    """Charge the required fees plus the declinable ones the shopper accepted.

    Every percent fee computes on the CONFIGURED subtotal (base plus option
    deltas) and never on another fee, so fees do not compound and their order
    cannot change the total. Returns the fee rows as charged and what they add to
    the WHOLE line.
    """
    charged = [f for f in fees if f.required or f.id in accepted]
    charged.sort(key=lambda f: f.id)

    quoted, total = [], 0
    for fee in charged:
        rate = None
        amount = fee.amount
        if fee.basis == PERCENT:
            rate = fee.amount
            base = subtotal_cents * quantity if fee.apply_to == PER_LINE else subtotal_cents
            amount = _percent_of(base, rate)
        extended = amount if fee.apply_to == PER_LINE else amount * quantity
        total += extended
        row = {
            "id": fee.id,
            "label": fee.label,
            "sku": fee.sku,
            "basis": fee.basis,
            "amount": amount,
            "apply_to": fee.apply_to,
        }
        if rate is not None:
            # Without the rate the snapshot records a number nobody can re-derive.
            row["rate"] = rate
        quoted.append(row)
    return quoted, total


def price_configured(
    base_unit_cents: int,
    option_sets,
    selections,
    tier_group: str | None = None,
    *,
    fees=(),
    accepted_fee_ids=(),
    base_sku: str = "",
    quantity: int = 1,
    sku_separator: str = DEFAULT_SKU_SEPARATOR,
) -> ConfiguredPrice:
    """Validate the selections against the authoritative catalog, then price one line.

    `option_sets` and `fees` are catalog rows the caller read from the database;
    the only fee fact a caller may pass is which DECLINABLE ones the shopper took.
    No caller-supplied price, delta or fee amount is ever honored (requirement 1.4).

    `fee_total_cents` is a LINE number (per-unit fees times quantity, per-line
    fees once) and is NOT folded into `unit_cents`, so a caller that ignores fees
    charges the configured product and nothing extra rather than double-counting.
    """
    quantity = max(1, quantity)
    accepted = set(accepted_fee_ids)
    if len(accepted) != len(tuple(accepted_fee_ids)):
        raise ComposeRefusal("duplicate accepted fee id")

    sets_by_id = {}
    for option_set in option_sets:
        if option_set.id in sets_by_id:
            raise ComposeRefusal(f"duplicate option set {option_set.id}")
        sets_by_id[option_set.id] = option_set

    picked = {}
    for selection in selections:
        if selection.set_id in picked:
            raise ComposeRefusal(f"duplicate selection for option set {selection.set_id}")
        picked[selection.set_id] = selection

    fees = list(fees)
    _validate(sets_by_id, picked, {f.id: f for f in fees}, accepted)

    chosen = sorted(
        (sets_by_id[i] for i in picked), key=lambda s: (s.sort_order, s.id)
    )
    unit_cents = base_unit_cents
    sku_parts = [base_sku] if base_sku else []
    lines, tier_applied = [], False
    for option_set in chosen:
        selection = picked[option_set.id]
        if option_set.prompt_type in TEXT_PROMPTS:
            lines.append(
                {
                    "option_set_id": option_set.id,
                    "option_set_name": option_set.display_name,
                    "text": selection.text.strip(),
                }
            )
            continue
        # Catalog order, never the order the browser happened to post: the
        # snapshot must be a function of WHAT was chosen, not of how it arrived.
        wanted = set(selection.value_ids)
        for value in sorted(
            (v for v in option_set.values if v.id in wanted),
            key=lambda v: (v.sort_order, v.id),
        ):
            delta, tiered = delta_for(value, tier_group)
            tier_applied = tier_applied or tiered
            unit_cents += delta
            if value.sku_fragment:
                sku_parts.append(value.sku_fragment)
            lines.append(
                {
                    "option_set_id": option_set.id,
                    "option_set_name": option_set.display_name,
                    "value_id": value.id,
                    "value_name": value.name,
                    "sku_fragment": value.sku_fragment,
                    "price_delta": delta,
                }
            )

    # AT OR BELOW nothing (requirement 1.3). A configured unit that comes to
    # exactly 0 is a broken catalog row, not a free product, and a positive fee
    # must not be able to carry it into a sale.
    if unit_cents <= 0:
        raise NegativeTotalError(unit_cents)

    quoted_fees, fee_total = _apply_fees(fees, accepted, unit_cents, quantity)

    line_total = unit_cents * quantity + fee_total
    if line_total <= 0:
        raise NegativeTotalError(line_total, line=True)

    return ConfiguredPrice(
        unit_cents=unit_cents,
        composite_sku=sku_separator.join(sku_parts),
        fee_total_cents=fee_total,
        snapshot={
            "version": SNAPSHOT_VERSION,
            "base_unit_cents": base_unit_cents,
            "tier_group": tier_group or None,
            # A tier_group that matched nothing prices at retail, which is
            # invisible in the money alone: the dealer is charged retail and the
            # order looks correct. Record whether the tier was honored.
            "tier_applied": tier_applied,
            "lines": lines,
            "unit_cents": unit_cents,
            "quantity": quantity,
            "fees": quoted_fees,
            "fee_total_cents": fee_total,
        },
    )


def minimum_configured_cents(base_unit_cents: int, option_sets) -> int:
    """The cheapest unit price these option sets can produce on this base.

    The floor, not a sample: what a shopper would pay if they answered every
    prompt the cheapest legal way. `price_configured` refuses at or below zero
    (requirement 1.3), so a floor at or below zero means the catalog carries a
    configuration that 422s at add-to-cart, which is the defect the merchant
    walk found. `OptionValue.clean()` checks this before the row is saved, so
    the refusal is a form error on the price field instead of a broken buy
    button nobody sees until a shopper hits it.

    Per set, the cheapest legal answer is:
      required choice_one   the lowest delta, because one must be picked;
      any choice_many       every negative delta, because all may be picked;
      optional choice_one   the lowest delta or nothing, whichever is lower;
      a text prompt         nothing, it carries no delta.
    Tier deltas are excluded on purpose: `delta_for` floors a tier row at the
    retail delta for positives and takes credits verbatim, so a tier row can
    price BELOW this floor. That is a dealer-catalog rule and belongs with the
    dealer rows, not on a retail value the merchant is editing.
    """
    floor = base_unit_cents
    for option_set in option_sets:
        deltas = [v.price_delta for v in option_set.values]
        if not deltas:
            continue
        if option_set.prompt_type in TEXT_PROMPTS:
            continue
        if option_set.prompt_type == CHOICE_MANY:
            floor += sum(d for d in deltas if d < 0)
        elif option_set.required:
            floor += min(deltas)
        else:
            floor += min(min(deltas), 0)
    return floor
