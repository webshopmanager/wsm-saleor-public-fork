# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Where a product may not go, answered once, from the destination on the order.

The rule this module enforces is a CARB part that is not legal in California,
and the shape it takes is a list of US state codes on the product plus an
optional allow-list of shipping zones. It answers exactly one question:

    is this destination serviced for these products?

**Fail SAFE, which here means do LESS** (Dana, 2026-09-09). A restriction that
cannot be decided does not block:

- a destination we cannot read (no address, no country, a `countryArea` a
  shopper typed by hand) is SERVICED. Blocking on an unreadable address turns
  one bad string into a store that takes no orders, and the money case it would
  protect is a part shipped to a state it is not legal in, which is a return,
  not a loss;
- a row that names no states and no zones restricts NOTHING. A half-filled
  compliance row is the likeliest merchant mistake on the screen, and the
  version of it that blocks every order in every state is the one failure this
  subsystem must never have.

The interim Compose service (wsm-app-platform `external_apps/compose/rules/
restrictions.py`, deleted with the rules subsystem in #181) failed CLOSED on
both, vetoing on an undecidable destination and treating an unscoped rule as
"everywhere". That was the pre-consumer design; today's ruling inverts it, and
the two tests named for it are `test_an_unreadable_destination_is_serviced` and
`test_a_row_that_names_nowhere_restricts_nothing`.

Nothing here is money, and nothing here runs on a shopper read path: the only
caller is the plugin at order creation.
"""

from dataclasses import dataclass

# Every `countryArea` code a US address can legitimately carry: the 50 states,
# DC, the territories and the armed-forces codes (ISO 3166-2:US). The admin
# validates a merchant's codes against this, so a typo is refused on the screen
# it was made on instead of quietly matching no destination forever.
US_SUBDIVISIONS = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "DC AS GU MP PR VI UM AA AE AP".split()
)


@dataclass(frozen=True)
class Refusal:
    """One product that cannot go where this order is going."""

    product_id: int
    message: str


def destination(address) -> tuple[str, str]:
    """(country code, state code) off a Saleor `Address`, uppercased.

    Empty strings for whatever is absent or unreadable, which is the input the
    fail-safe branches key on.
    """
    if address is None:
        return "", ""
    country = getattr(address, "country", None)
    # `CountryField` hands back a `Country`; a plain string is what a test or a
    # webhook payload holds.
    code = getattr(country, "code", country) or ""
    return str(code).strip().upper(), str(
        getattr(address, "country_area", "") or ""
    ).strip().upper()


def _zone_countries(zone) -> set[str]:
    return {
        str(getattr(country, "code", country)).strip().upper()
        for country in (zone.countries or [])
    }


def is_destination_serviced(product_ids, address) -> tuple[bool, list[Refusal]]:
    """Answer whether every one of these products can ship to this address.

    Returns (serviced, refusals). `refusals` is empty whenever serviced is True,
    and carries one legible sentence per refused product otherwise.

    Cost: ONE indexed query when no product in the order has a compliance row,
    which is the normal order, and a second one for the zone allow-list only
    when a row exists and names zones.
    """
    from .models import ProductCompliance

    ids = {int(pk) for pk in product_ids if pk}
    if not ids:
        return True, []
    rows = list(
        ProductCompliance.objects.filter(product_id__in=ids).prefetch_related(
            "include_shipping_zones"
        )
    )
    if not rows:
        return True, []

    country, state = destination(address)
    refusals: list[Refusal] = []
    for row in rows:
        zones = list(row.include_shipping_zones.all())
        states = row.state_codes
        if not country:
            # An unreadable destination is serviced: see the module docstring.
            continue
        if states and country == "US" and state in states:
            refusals.append(Refusal(row.product_id, row.refusal_message(state)))
            continue
        if zones and not any(country in _zone_countries(zone) for zone in zones):
            refusals.append(Refusal(row.product_id, row.refusal_message(country)))
    return not refusals, refusals
