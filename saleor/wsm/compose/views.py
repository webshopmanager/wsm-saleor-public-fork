# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The two REST endpoints wsm-storefront develop already speaks.

Endpoint 1 is catalog data and is public, like the product it decorates.
Endpoint 2 prices the line SERVER-SIDE and writes it through Saleor's own
``add_variants_to_checkout``, the same function ``checkoutLinesAdd`` calls, so
``price_expiration`` invalidation and the price recalculation happen exactly as
stock. Nothing here writes a CheckoutLine row by hand.

Endpoint 2's caller is the storefront SERVER, and `X-Compose-Key` is how it
proves that (saleor/wsm/http.py). The forgeable thing was never the price, which
is computed here from catalog rows: it is the checkout the line lands in.
`X-Client-Id` and `X-Saleor-Domain` still arrive and are still ignored, because
one process serves one tenant. Endpoint 1 is catalog data and stays open.
"""

import base64
import binascii
import json
import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.sites.models import Site
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from ...checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ...checkout.models import Checkout, CheckoutLine
from ...checkout.utils import add_variants_to_checkout, invalidate_checkout
from ...core.db.connection import allow_writer
from ...core.utils.metadata_manager import MetadataItem
from ...graphql.checkout.mutations.utils import CheckoutLineData
from ...plugins.manager import get_plugins_manager
from ...product.models import Product, ProductVariant, ProductVariantChannelListing
from ..dealer import pricing as dealer_pricing
from ..dealer.no_stacking import LINE_METADATA_KEY as DEALER_KEY
from ..dealer.tax import bind_tax_exemption
from ..checkout import LineRefused, check_addable, whole_number
from ..http import storefront_key_required
from . import pricing
from .models import Fee, OptionSet, to_cents

# Line metadata keys. The storefront reads these; they are contract, not detail.
META_OPTIONS = "wsm.options"
META_SKU = "wsm.options.sku"
META_CID = "wsm.options.cid"
META_ACCEPTED = "wsm.options.acc"
META_FEE = "compose.fee"
META_PARENT = "compose.parent_line"

PRICE_OVERRIDE_REASON = "wsm.compose"

# The keys MP3 prices from. Their authority copy is written to PRIVATE metadata
# (stock Saleor lets any unauthenticated caller write PUBLIC line metadata:
# saleor/graphql/meta/permissions.py maps CheckoutLine to `no_permissions`), and
# the public copies above stay because the storefront cart and order screens
# read them for display and B6 is "no storefront code".
PRICED_FROM = (META_OPTIONS, META_CID, META_ACCEPTED, META_FEE, META_PARENT)


def _money(cents: int) -> str:
    """Cents to the two-decimal string the storefront parses, sign carried in it."""
    return f"{Decimal(cents) / 100:.2f}"


def _from_gid(raw: str, expected: str) -> str | None:
    """The primary key inside a Saleor global id, or None if it is not one of `expected`.

    Padding is restored before decoding: a GID that lost its `=` in a URL is a
    routine thing to receive and refusing it would be a false 404.
    """
    if not raw:
        return None
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    type_name, sep, pk = decoded.partition(":")
    if not sep or type_name != expected or not pk:
        return None
    return pk


def _not_found(what: str):
    return JsonResponse({"violations": [f"unknown {what}"]}, status=404)


def _tier_group(customer_gid, db):
    """The buyer group behind a customer id, resolved by wsm.dealer, or None.

    Compose never reads `DealerCustomer` itself: one string names one buyer
    group across both apps (wsm.dealer models docstring) and the app that owns
    the row owns the lookup.
    """
    return dealer_pricing.tier_group_for(
        _from_gid(customer_gid or "", "User"), database_connection_name=db
    )


@require_GET
def option_sets(request, product_gid):
    """Everything the PDP needs to draw the configurator, in three queries.

    Sets and their values come back through one prefetch; fees are a second
    table and a third query. The existence check on the product is paid ONLY
    when the product carries no configuration, which is the case the storefront
    never asks about: a configured product costs three queries, not four.

    RETAIL deltas, for everyone. This is the fork's one endpoint that answers
    without the storefront key, and it used to take a `?customerId=` and quote
    that customer's dealer deltas: user ids are sequential integers inside a
    guessable global id, so the whole dealer price book was readable one
    customer at a time by anyone who could reach the PDP. A dealer's own deltas
    come back from the key-gated add instead, which is where the money is taken
    and where the caller has already been authenticated.
    """
    product_pk = _from_gid(product_gid, "Product")
    if product_pk is None or not product_pk.isdigit():
        return _not_found("product")

    replica = settings.DATABASE_CONNECTION_REPLICA_NAME
    sets = list(
        OptionSet.objects.using(replica)
        .filter(product_id=product_pk)
        .prefetch_related("values")
    )
    fees = list(Fee.objects.using(replica).filter(product_id=product_pk))

    if not sets and not fees:
        if not Product.objects.using(replica).filter(pk=product_pk).exists():
            return _not_found("product")

    return JsonResponse(
        {
            "data": [
                {
                    "id": s.pk,
                    "name": s.name,
                    "label": s.label,
                    "prompt_type": s.prompt_type,
                    "required": s.required,
                    "note": s.note,
                    "values": [
                        {
                            "id": v.pk,
                            "name": v.name,
                            "sku_fragment": v.sku_fragment,
                            "price_delta": f"{v.price_delta:.2f}",
                            "image_url": v.image_url,
                        }
                        for v in s.values.all()
                    ],
                }
                for s in sets
            ],
            "fees": [
                {
                    "id": f.pk,
                    "label": f.label,
                    "sku": f.sku,
                    "basis": f.basis,
                    "amount": f"{f.amount:.2f}",
                    "apply_to": f.apply_to,
                    "required": f.required,
                    "decline_label": f.decline_label,
                }
                for f in fees
            ],
        }
    )


def _selections(raw):
    out = []
    for entry in raw or []:
        set_id = entry.get("set_id")
        # The storefront keys its selections map by option-set id, so JSON hands
        # the id back as a STRING ("1"), never an int: Object.entries on a
        # JS object has no other shape. Refusing it 422s every configured add
        # from wsm-storefront develop, which is the one caller this contract
        # exists for. Digits only, so a set id the caller invented is still
        # refused by the pricing engine and never coerced into one.
        if isinstance(set_id, str) and set_id.isdigit():
            set_id = int(set_id)
        if not isinstance(set_id, int) or isinstance(set_id, bool):
            raise pricing.UnknownValueError(f"selection has no option set id: {entry!r}")
        out.append(
            pricing.Selection(
                set_id=set_id,
                value_ids=tuple(entry.get("value_ids") or ()),
                text=entry.get("text") or "",
            )
        )
    return out


@csrf_exempt
@require_POST
@storefront_key_required
@allow_writer()
def configured_line(request):
    """Price one configuration and put it in the checkout as a priced line.

    The caller sends WHAT was chosen. Every number comes from our own tables.
    """
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"violations": ["body is not JSON"]}, status=422)

    checkout_token = _from_gid(body.get("checkoutId", ""), "Checkout")
    product_pk = _from_gid(body.get("productId", ""), "Product")
    variant_pk = _from_gid(body.get("variantId", ""), "ProductVariant")
    if checkout_token is None:
        return _not_found("checkout")
    if product_pk is None or variant_pk is None:
        return _not_found("product")

    quantity = whole_number(body.get("quantity") or 1)
    if quantity is None:
        return JsonResponse(
            {"violations": ["quantity must be a whole number"]}, status=422
        )
    if quantity < 1:
        return JsonResponse({"violations": ["quantity must be at least 1"]}, status=422)

    checkout = Checkout.objects.filter(token=checkout_token).first()
    if checkout is None:
        return _not_found("checkout")

    variant = (
        ProductVariant.objects.filter(pk=variant_pk, product_id=product_pk)
        .select_related("product")
        .first()
    )
    if variant is None:
        return _not_found("product")

    listing = ProductVariantChannelListing.objects.filter(
        variant_id=variant.pk, channel_id=checkout.channel_id
    ).first()
    if listing is None or listing.price_amount is None:
        return _not_found("product")

    sets = OptionSet.objects.filter(product_id=product_pk).prefetch_related(
        "values", "values__tier_deltas"
    )
    fees = list(Fee.objects.filter(product_id=product_pk))
    # Read from the WRITER: a group that decides the price about to be stamped
    # on a checkout line is read from the database the line is written to.
    customer_pk = _from_gid(body.get("customerId") or "", "User")
    tier_group = _tier_group(
        body.get("customerId"), settings.DATABASE_CONNECTION_DEFAULT_NAME
    )

    requested_fee_ids = tuple(body.get("acceptedFeeIds") or ())
    # A required fee is charged whether or not the caller names it, and the
    # engine refuses the id because "accepting" it is meaningless. The
    # storefront posts back every fee the shopper was shown, required ones
    # included, so that redundancy is normalised here on the wire rather than
    # costing a shopper their checkout. An id naming NO fee still fails.
    required_fee_ids = {f.pk for f in fees if f.required}
    accepted_fee_ids = tuple(
        fid for fid in requested_fee_ids if fid not in required_fee_ids
    )

    try:
        selections = _selections(body.get("selections"))
        priced = pricing.price_configured(
            to_cents(listing.price_amount),
            [s.to_pricing() for s in sets],
            selections,
            tier_group,
            fees=[f.to_pricing() for f in fees],
            accepted_fee_ids=accepted_fee_ids,
            base_sku=variant.sku or "",
            quantity=quantity,
        )
    except pricing.ComposeRefusal as refusal:
        return JsonResponse({"violations": [str(refusal)]}, status=422)

    cid = str(uuid.uuid4())
    charged = {row["id"]: row for row in priced.snapshot["fees"]}
    fees_by_id = {f.pk: f for f in fees}

    variants = [variant]
    stamps_by_variant = {}
    lines_data = [
        CheckoutLineData(
            variant_id=str(variant.pk),
            quantity=quantity,
            quantity_to_update=True,
            custom_price=Decimal(priced.unit_cents) / 100,
            custom_price_to_update=True,
            custom_price_reason=PRICE_OVERRIDE_REASON,
            custom_price_reason_to_update=True,
            metadata_list=[
                MetadataItem(META_OPTIONS, json.dumps(priced.snapshot)),
                MetadataItem(META_SKU, priced.composite_sku),
                MetadataItem(META_CID, cid),
                MetadataItem(
                    META_ACCEPTED, json.dumps(sorted(requested_fee_ids))
                ),
            ],
        )
    ]
    stamps_by_variant[variant.pk] = {
        item.key: item.value
        for item in lines_data[0].metadata_list
        if item.key in PRICED_FROM
    }
    if priced.snapshot.get("tier_applied"):
        # A configured line that took a tier IS a dealer line. MP1 and MP2 find
        # one by the presence of this key and nothing else, so without it a
        # voucher or a catalogue promotion comes off a price that is already the
        # dealer's. MP3 rewrites it on every recalculation (`_mark_dealer`).
        stamps_by_variant[variant.pk][DEALER_KEY] = json.dumps(
            {"group": priced.snapshot.get("tier_group") or ""}
        )

    for fee_id, row in sorted(charged.items()):
        fee = fees_by_id[fee_id]
        fee_variant = fee.ensure_variant(checkout.channel)
        variants.append(fee_variant)
        lines_data.append(
            CheckoutLineData(
                variant_id=str(fee_variant.pk),
                # A per-unit fee is one fee line of `quantity`, a per-line fee is
                # one of 1. The fee's own unit price is the same either way, so
                # the line total is the fee total the pricing engine reported.
                quantity=quantity if row["apply_to"] == pricing.PER_UNIT else 1,
                quantity_to_update=True,
                custom_price=Decimal(row["amount"]) / 100,
                custom_price_to_update=True,
                custom_price_reason=PRICE_OVERRIDE_REASON,
                custom_price_reason_to_update=True,
                metadata_list=[
                    MetadataItem(
                        META_FEE,
                        json.dumps({"label": row["label"], "apply_to": row["apply_to"]}),
                    ),
                    MetadataItem(META_CID, cid),
                    # The PARENT is named by its cid, not its line pk: the
                    # storefront pairs a fee to its product on
                    # `compose.parent_line == the parent's wsm.options.cid`
                    # (composeFee.ts feeLinesFor) and on nothing else. A pk
                    # here orphaned every fee in the cart ("the item this
                    # charge applies to is no longer in your cart") AND left
                    # it out of withChildLines, so a per-unit crate charge
                    # stayed at one unit's money when the shopper bought two.
                    # Writing the cid also makes the pairing knowable BEFORE
                    # the insert, which is why the post-write lookup below is
                    # gone.
                    MetadataItem(META_PARENT, cid),
                ],
            )
        )
        stamps_by_variant[fee_variant.pk] = {
            item.key: item.value
            for item in lines_data[-1].metadata_list
            if item.key in PRICED_FROM
        }

    manager = get_plugins_manager(allow_replica=False)
    checkout_info = fetch_checkout_info(checkout, [], manager)
    site_settings = Site.objects.get_current().settings
    try:
        # The checks `checkoutLinesAdd` runs before the identical write. Fee
        # variants are in the set on purpose: they are published, available and
        # untracked, so they pass, and their quantity still counts towards the
        # shop's per-checkout limit exactly as it does through the mutation.
        check_addable(
            checkout,
            checkout_info.channel,
            variants,
            lines_data,
            site_settings=site_settings,
            delivery_method_info=checkout_info.get_delivery_method_info(),
        )
    except LineRefused as refusal:
        return JsonResponse({"violations": refusal.violations}, status=422)
    add_variants_to_checkout(
        checkout,
        variants,
        lines_data,
        checkout_info.channel,
        replace=False,
        replace_reservations=True,
        # Reservations are a stock feature we do not turn on for the bake-off;
        # passing a length here would reserve stock for the hidden fee variants too.
        reservation_length=None,
        calculate_stocks_with_shipping_zones=(
            site_settings.use_legacy_shipping_zone_stock_availability
        ),
    )

    lines, _ = fetch_checkout_lines(checkout)
    checkout_info.lines = lines
    # `add_variants_to_checkout` only ever stores `metadata_list` publicly, so
    # the copy MP3 actually prices from is written here, on the lines this
    # request just wrote, and on no others: promoting whatever happens to be in
    # a line's public metadata would honour exactly the forgery this closes.
    stamped = []
    for info in lines:
        values = stamps_by_variant.get(info.line.variant_id)
        if values:
            info.line.store_value_in_private_metadata(values)
            stamped.append(info.line)
    if stamped:
        CheckoutLine.objects.bulk_update(stamped, ["private_metadata"])

    invalidate_checkout(checkout_info, lines, manager, save=True)

    # A configured line that took a tier IS a dealer line, so the rest of the
    # buyer's account follows it here too: a tax-exempt dealer whose whole cart
    # is configured products is exempt on the route that priced them, and not
    # only when they also happen to add a plain SKU.
    bind_tax_exemption(
        checkout,
        customer_pk,
        database_connection_name=settings.DATABASE_CONNECTION_DEFAULT_NAME,
    )

    return JsonResponse(
        {
            "checkoutId": body.get("checkoutId"),
            "unitPrice": _money(priced.unit_cents),
            "compositeSku": priced.composite_sku,
            "feeTotal": _money(priced.fee_total_cents),
        }
    )
