# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""The two REST endpoints wsm-storefront develop already speaks.

Endpoint 1 is catalog data and is public, like the product it decorates.
Endpoint 2 prices the line SERVER-SIDE and writes it through Saleor's own
``add_variants_to_checkout``, the same function ``checkoutLinesAdd`` calls, so
``price_expiration`` invalidation and the price recalculation happen exactly as
stock. Nothing here writes a CheckoutLine row by hand.

The storefront still sends X-Client-Id, X-Saleor-Domain and X-Compose-Key. They
are read by nobody: signing existed to make an out-of-process price trustworthy,
and the price is now computed in this process from catalog rows. The headers are
deleted from the contract in the storefront's own time (design section 2).
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
from ...checkout.models import Checkout
from ...checkout.utils import (
    add_variants_to_checkout,
    checkout_lines_bulk_update,
    invalidate_checkout,
)
from ...core.db.connection import allow_writer
from ...core.utils.metadata_manager import MetadataItem
from ...graphql.checkout.mutations.utils import CheckoutLineData
from ...plugins.manager import get_plugins_manager
from ...product.models import Product, ProductVariant, ProductVariantChannelListing
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


@require_GET
def option_sets(request, product_gid):
    """Everything the PDP needs to draw the configurator, in three queries.

    Sets and their values come back through one prefetch; fees are a second
    table and a third query. The existence check on the product is paid ONLY
    when the product carries no configuration, which is the case the storefront
    never asks about: a configured product costs three queries, not four.
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
        if not isinstance(set_id, int):
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

    quantity = int(body.get("quantity") or 1)
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
            # U3 resolves the buyer's tier group here. Retail until then.
            None,
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
                ],
            )
        )

    manager = get_plugins_manager(allow_replica=False)
    checkout_info = fetch_checkout_info(checkout, [], manager)
    site_settings = Site.objects.get_current().settings
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

    # The product line's id is only knowable after the write, and the fee lines
    # must point at it. `cid` is what pairs them, so the lines we just wrote are
    # found in the list this recalculation already loaded: no extra read.
    ours = [li.line for li in lines if li.line.metadata.get(META_CID) == cid]
    parent = next((line for line in ours if META_OPTIONS in line.metadata), None)
    fee_lines = [line for line in ours if parent and line.pk != parent.pk]
    if fee_lines:
        for line in fee_lines:
            line.store_value_in_metadata({META_PARENT: str(parent.pk)})
        checkout_lines_bulk_update(fee_lines, ["metadata"])

    invalidate_checkout(checkout_info, lines, manager, save=True)

    return JsonResponse(
        {
            "checkoutId": body.get("checkoutId"),
            "unitPrice": _money(priced.unit_cents),
            "compositeSku": priced.composite_sku,
            "feeTotal": _money(priced.fee_total_cents),
        }
    )
