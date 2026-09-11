# WSM-FORK: fork-owned file. See docs/wsm/CORE-TOUCHES.md.
"""Vehicle in, this container's assortment out, in a FIXED number of calls.

Dana, 2026-09-10, verbatim: "There's either a product series and a bundle in a
kit or bundle. That product series is getting you down to one product, although
sometimes there are two choices when you get down there. A bundle is an
assortment of products, but they're still all relative to fitment, at least some
of them are."

Three sentences, three rules in this module:

* "getting you down to one product" -- a slot that carries a `partitioning_axis`
  drills; a slot without one takes everything that survives. Read off the data,
  never off a toggle.
* "sometimes there are two choices when you get down there" -- a slot resolves
  to a SHORT LIST, not always to one. One survivor auto-selects and says so;
  more than one is presented in the slot's own axis order.
* "at least some of them are" -- a candidate the engine holds with no fitment
  data at all is UNIVERSAL and is kept in a vehicle-resolved slot. Dropping it
  would refuse to sell the belt-temp gauge to anybody with a truck.

COST. Two engine round trips when a vehicle is given, ONE Saleor read of three
queries, and both numbers are independent of the member count and of the slot
count: 20 slots of 5 candidates costs exactly what 1 slot of 1 candidate costs.
Never one call per member, which is the shape this module exists to refuse.

WHY TWO ENGINE CALLS AND NOT ONE. `fitment_pairs` is a `should` over the nested
per-combination match and `term(is_universal, true)`
(`search_query.go:fitmentPairsClause`), so the vehicle call answers "fits, or
the merchant explicitly claimed universal" and DROPS everything that is merely
`fitment_unknown`. It also cannot tell a product the engine has indexed and
rejected from one the engine has never seen, and a member with a backdated
`updatedAt` is permanently absent from the index (measured on the TNO stage,
2026-09-11). So the vehicle-free call is what supplies the engine's own view of
the container: which products it knows, and which of those carry no fitment at
all. The two calls are the minimum that can answer the question without lying;
both depend only on (collections, vehicle), both are public GETs, and both are
cacheable at the CDN by URL.

DEFAULT-DENY. Unconfigured, unreachable and unparseable all do LESS, never more.
No engine URL configured, a timeout, a non-200, a body that is not the expected
JSON: every one of them refuses the container rather than showing every
candidate as though it fitted. A non-numeric collection id is never sent at all,
because `collection_ids` fails OPEN on one -- a base64 global id passed straight
through returned 46,380 products on the stage instead of five.
"""

import base64
import logging
import os
from dataclasses import dataclass, field, replace

import requests
from django.conf import settings

from .models import ContainerSlot, KitConfig, KitMember

ENGINE_PATH = "/api/search/products"

# The deploy knob, in the module that uses it, because this fork's budget of
# edited core files is a decision somebody has to make in a diff and a search URL
# is not worth spending one on. A Django setting of the same name WINS when a
# deployment defines one, the environment answers when it does not, and unset is
# the fail-SAFE state: no engine means no vehicle resolution at all, never a
# container that shows every candidate as though it fitted.
ENGINE_URL_DEFAULT = os.environ.get("WSM_SEARCH_ENGINE_URL", "")
# A shopper is waiting on this call. Past the timeout the container refuses
# rather than hangs; the engine answers a collection filter in single-digit
# milliseconds, so this is a ceiling and never a budget.
ENGINE_TIMEOUT_DEFAULT = float(os.environ.get("WSM_SEARCH_ENGINE_TIMEOUT", "4.0"))

# WARNING and not ERROR: an engine that will not answer is a REMOTE failure, and
# the severity says who owns it. `%s` interpolation because the fork's log
# formatter drops `extra={}`.
logger = logging.getLogger(__name__)

# What the engine says about one candidate's product, for THIS vehicle.
FITS = "fits"
# Indexed, carries no fitment rows and no merchant flag either way. Kept: "at
# least some of them are" relative to fitment, so the rest are not.
UNIVERSAL = "universal"
# The engine has never seen this product. Our indexing gap, not a fitment fact,
# so it is kept and FLAGGED and never rendered as "does not fit".
UNKNOWN = "unknown"
# Before a vehicle is entered there is nothing to say. Zero engine calls.
UNFILTERED = "unfiltered"

# Refusal codes. The code is what a storefront branches on; the sentence beside
# it is the merchant's own and changes without the code moving.
REFUSAL_NOT_A_CONTAINER = "container_not_configured"
REFUSAL_SLOT_HAS_NO_FIT = "container_slot_has_no_fit"
REFUSAL_ENGINE_UNAVAILABLE = "container_engine_unavailable"
REFUSAL_BAD_COLLECTION_ID = "container_bad_collection_id"
REFUSAL_TRUNCATED = "container_too_large_to_resolve"

DEFAULT_MISS_MESSAGE = "We have nothing for your vehicle in this bundle."

# The engine pages at 20 by default, and a container with more members than the
# page would come back short: the missing docs would read as "does not fit",
# which is the silent-failure shape this whole module is written against. The
# page is asked for at the size of the candidate set, and a `pagination.total`
# larger than what came back is a REFUSAL rather than a shrug.
ENGINE_PAGE_HEADROOM = 10


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str
    slot_label: str = ""


@dataclass(frozen=True)
class Candidate:
    """One thing that could fill a slot, and what the engine says about it."""

    variant: object
    product_id: int
    quantity: int
    sort_order: int
    fitment: str
    # The row a merchant typed. None when the candidate was DERIVED from a
    # slot's `source_collection`, which is how a kit holds a series without
    # copying its members and going stale the next time the series grows.
    member: object | None = None
    # The channel this candidate was resolved in, carried to the leaf that
    # needs it: stock's `ProductVariant.pricing` answers null without one, and
    # a candidate with no price is a candidate nobody can buy. Blank is the
    # merchant preview, and blank prices nothing rather than pricing wrongly.
    channel_slug: str = ""

    @property
    def variant_id(self) -> int:
        return self.variant.pk


@dataclass(frozen=True)
class ResolvedSlot:
    slot: object
    candidates: list
    # How many candidates the vehicle ruled out. Reported rather than dropped
    # in silence, because a slot that quietly lost four of its five options
    # looks identical to a slot that only ever had one.
    excluded: int = 0

    @property
    def drills(self) -> bool:
        return bool(self.slot.partitioning_axis)

    @property
    def choose(self) -> bool:
        """Whether the shopper still has a decision to make in this slot."""
        return self.drills and len(self.candidates) > 1

    @property
    def selected(self) -> list:
        """What goes in the cart with no further input from the shopper.

        A drilling slot with exactly one survivor auto-selects it (Dana's open
        decision 2, default), and says so by being `choose == False` with one
        candidate. A drilling slot with several selects nothing until asked. A
        slot that does not drill takes every survivor, which is what every kit
        authored before slots existed does, and is why the backfill moves no
        money.
        """
        if not self.drills:
            return list(self.candidates)
        return list(self.candidates) if len(self.candidates) == 1 else []


@dataclass(frozen=True)
class Resolution:
    kit: object
    vehicle: str
    slots: list = field(default_factory=list)
    refusal: object | None = None
    engine_calls: int = 0
    # See `Candidate.channel_slug`. Held here as well because the kit's own
    # members hang off the resolution and reach the same priced leaf.
    channel_slug: str = ""

    @property
    def refused(self) -> bool:
        return self.refusal is not None

    def selected_variant_ids(self) -> list:
        """The picks, in the shape `KitConfig.pricing_members` already takes.

        There is no second pricing path: the resolved assortment is a list of
        variant ids, the kit discount prorates over exactly those, and dealer
        tiers apply per line because the lines are real.
        """
        return [
            candidate.variant_id
            for resolved in self.slots
            for candidate in resolved.selected
        ]


class _EngineUnavailable(Exception):
    """Internal: the engine could not be asked, or could not be believed."""


def _numeric_pk(value) -> int:
    """A collection id the engine can be trusted with, or nothing at all.

    `collection_ids` FAILS OPEN on a value it cannot parse: a base64 global id
    passed through returned 46,380 products on the TNO stage where the container
    holds five. So this is the only door, and it is closed by default.
    """
    if isinstance(value, bool):
        raise ValueError("a boolean is not a collection id")
    if isinstance(value, int):
        pk = value
    elif isinstance(value, str) and value.isdigit():
        pk = int(value)
    else:
        raise ValueError(f"not a numeric collection id: {value!r}")
    if pk < 1:
        raise ValueError(f"not a numeric collection id: {value!r}")
    return pk


def _product_pk(global_id: str) -> int | None:
    """`UHJvZHVjdDo3ODcx` -> 7871. Anything else is None, and is skipped."""
    try:
        decoded = base64.b64decode(global_id).decode()
        type_name, _, raw = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    if type_name != "Product" or not raw.isdigit():
        return None
    return int(raw)


def _engine_documents(collection_pks, fitment_pairs, expected) -> dict:
    """One GET. Returns {product_pk: document}, or refuses the whole container.

    `expected` sizes the page: the engine defaults to 20 and a short page would
    read as "the rest do not fit". A total larger than the page still came back
    is refused out loud rather than resolved against a partial answer.
    """
    base = (
        getattr(settings, "WSM_SEARCH_ENGINE_URL", ENGINE_URL_DEFAULT) or ""
    ).rstrip("/")
    if not base:
        raise _EngineUnavailable("no search engine is configured")

    params = {
        "collection_ids": ",".join(str(pk) for pk in sorted(collection_pks)),
        "page": 1,
        "per_page": max(expected, 1) + ENGINE_PAGE_HEADROOM,
    }
    if fitment_pairs:
        params["fitment_pairs"] = fitment_pairs

    try:
        response = requests.get(
            base + ENGINE_PATH,
            params=params,
            timeout=getattr(
                settings, "WSM_SEARCH_ENGINE_TIMEOUT", ENGINE_TIMEOUT_DEFAULT
            ),
        )
        response.raise_for_status()
        payload = response.json()
        documents = payload["products"]
        total = payload.get("pagination", {}).get("total", len(documents))
    except (requests.RequestException, ValueError, KeyError, TypeError) as problem:
        raise _EngineUnavailable(str(problem)) from problem

    if total > len(documents):
        raise _EngineUnavailable(
            f"the engine holds {total} products in this container and returned "
            f"{len(documents)}"
        )

    by_pk = {}
    for document in documents:
        pk = _product_pk(document.get("id", ""))
        if pk is not None:
            by_pk[pk] = document
    return by_pk


def _load_container(collection_pk, database):
    """The container, its slots and every listed candidate, in THREE queries.

    Three and not three-plus-one-per-slot: the candidates are prefetched across
    every slot at once, with the variant joined, so a 20-slot container reads
    exactly what a 1-slot container reads.

    Every queryset names its connection, including the prefetches: under
    ENABLE_RESTRICT_WRITER_MIDDLEWARE an unrouted read inside a GraphQL request
    raises, and a prefetch queryset does NOT inherit the parent's `using`.
    """
    from django.db.models import Prefetch

    return (
        KitConfig.objects.using(database)
        .select_related("collection")
        .prefetch_related(
            Prefetch(
                "slots",
                queryset=ContainerSlot.objects.using(database).select_related(
                    "source_collection"
                ),
            ),
            Prefetch(
                "slots__candidates",
                queryset=KitMember.objects.using(database).select_related("variant"),
            ),
        )
        .filter(collection_id=collection_pk)
        .first()
    )


def _derived_candidates(source_pks, database):
    """Every derived slot's candidates, from ONE query, keyed by collection.

    One candidate per PRODUCT, because the engine's document IS a product and
    the axis picks the variant afterwards from what the page already holds.
    Filtering on the annotation rather than on the relation keeps this to a
    single join, and therefore to a single query however many slots share it.
    """
    from django.db.models import F

    from ...product.models import Product

    by_collection: dict = {}
    if not source_pks:
        return by_collection

    rows = (
        Product.objects.using(database)
        .annotate(source_collection_id=F("collections__id"))
        .filter(source_collection_id__in=list(source_pks))
        .select_related("default_variant")
        .order_by("pk")
    )
    for product in rows:
        by_collection.setdefault(product.source_collection_id, []).append(product)
    return by_collection


def _slot_candidates(slot, derived, channel_slug=""):
    """What could fill this slot, and how many were unbuyable. Its own rows win.

    A slot that both lists candidates and names a source collection is a
    merchant mid-edit, and the rows they typed are the more specific answer.

    The second number is products in a derived collection with no default
    variant: nothing to put in a cart, so nothing to offer, but COUNTED, because
    a slot that quietly shrank looks exactly like a slot that was always small.
    """
    rows = list(slot.candidates.all())
    if rows:
        return [
            Candidate(
                variant=member.variant,
                product_id=member.variant.product_id,
                quantity=member.quantity,
                sort_order=member.sort_order,
                fitment=UNFILTERED,
                member=member,
                channel_slug=channel_slug,
            )
            for member in rows
        ], 0
    if slot.source_collection_id is None:
        return [], 0

    candidates = []
    unbuyable = 0
    for order, product in enumerate(derived.get(slot.source_collection_id, [])):
        if product.default_variant_id is None:
            unbuyable += 1
            continue
        candidates.append(
            Candidate(
                variant=product.default_variant,
                product_id=product.pk,
                quantity=slot.quantity,
                sort_order=order,
                fitment=UNFILTERED,
                member=None,
                channel_slug=channel_slug,
            )
        )
    return candidates, unbuyable


def _fitment_of(product_id, known, fitting) -> str:
    if product_id in fitting:
        document = fitting[product_id]
        return UNIVERSAL if document.get("fitment_unknown") else FITS
    if product_id not in known:
        # Never indexed. "We have never seen this part" is not "this part does
        # not fit your truck", so it is kept and flagged.
        return UNKNOWN
    return UNIVERSAL if known[product_id].get("fitment_unknown") else ""


def resolve(collection, fitment_pairs="", database=None, channel="") -> Resolution:
    """The container on this collection, resolved for this vehicle.

    `fitment_pairs` is the storefront's own vehicle string, `1:18,2:2008,3:2784`,
    passed through untouched: the engine owns what an axis id means and this
    module never reinterprets one. Empty means no vehicle yet, which is the
    price-range state, and it costs zero engine calls.

    `database` is the connection every read here names. A GraphQL caller passes
    the replica it was routed to; the REST kit path and a management command
    pass nothing and get the writer, which is what they already use.

    `channel` is the channel slug the shopper is asking in. It selects nothing
    and filters nothing here: it rides along so the GraphQL leaf can hand stock
    its own `ChannelContext` and stock can price the variant. Blank is the
    merchant preview and prices nothing, which is the fail-SAFE side.
    """
    database = database or settings.DATABASE_CONNECTION_DEFAULT_NAME
    channel = channel or ""
    try:
        collection_pk = _numeric_pk(getattr(collection, "pk", collection))
    except ValueError as problem:
        return Resolution(
            kit=None,
            vehicle=fitment_pairs or "",
            refusal=Refusal(REFUSAL_BAD_COLLECTION_ID, str(problem)),
            channel_slug=channel,
        )

    kit = _load_container(collection_pk, database)
    if kit is None:
        return Resolution(
            kit=None,
            vehicle=fitment_pairs or "",
            refusal=Refusal(
                REFUSAL_NOT_A_CONTAINER, "this collection is not sold as a container"
            ),
            channel_slug=channel,
        )

    slots = list(kit.slots.all())
    source_pks = set()
    bad_source = None
    for slot in slots:
        if slot.source_collection_id is None:
            continue
        try:
            source_pks.add(_numeric_pk(slot.source_collection_id))
        except ValueError as problem:  # pragma: no cover - a pk is always numeric
            bad_source = str(problem)
    if bad_source:
        return Resolution(
            kit=kit,
            vehicle=fitment_pairs or "",
            refusal=Refusal(REFUSAL_BAD_COLLECTION_ID, bad_source),
            channel_slug=channel,
        )

    derived = _derived_candidates(source_pks, database)
    per_slot = [(slot, *_slot_candidates(slot, derived, channel)) for slot in slots]

    engine_calls = 0
    known: dict = {}
    fitting: dict = {}
    if fitment_pairs:
        expected = sum(len(candidates) for _slot, candidates, _skipped in per_slot)
        collection_pks = {collection_pk} | source_pks
        try:
            known = _engine_documents(collection_pks, "", expected)
            engine_calls += 1
            fitting = _engine_documents(collection_pks, fitment_pairs, expected)
            engine_calls += 1
        except _EngineUnavailable as problem:
            # Default-deny: we cannot say what fits, so we do not sell. The
            # shopper gets the merchant's sentence; the reason goes to the log,
            # because "this bundle refused all afternoon" has to be diagnosable.
            logger.warning(
                "container %s could not be resolved for %s: %s",
                collection_pk,
                fitment_pairs,
                problem,
            )
            return Resolution(
                kit=kit,
                vehicle=fitment_pairs,
                refusal=Refusal(
                    REFUSAL_ENGINE_UNAVAILABLE,
                    kit.miss_message or DEFAULT_MISS_MESSAGE,
                ),
                engine_calls=engine_calls,
                channel_slug=channel,
            )

    resolved_slots = []
    for slot, candidates, unbuyable in per_slot:
        if not fitment_pairs:
            resolved_slots.append(
                ResolvedSlot(slot=slot, candidates=candidates, excluded=unbuyable)
            )
            continue
        survivors = []
        excluded = unbuyable
        for candidate in candidates:
            verdict = _fitment_of(candidate.product_id, known, fitting)
            if not verdict:
                excluded += 1
                continue
            # `replace` rather than a re-listing of every field: a candidate
            # that survives is the same candidate with the engine's verdict on
            # it, and a field added to `Candidate` must not have to be
            # remembered in a second place to survive the vehicle.
            survivors.append(replace(candidate, fitment=verdict))
        if slot.required and not survivors:
            return Resolution(
                kit=kit,
                vehicle=fitment_pairs,
                refusal=Refusal(
                    REFUSAL_SLOT_HAS_NO_FIT,
                    slot.miss_message or kit.miss_message or DEFAULT_MISS_MESSAGE,
                    slot_label=slot.label,
                ),
                engine_calls=engine_calls,
                channel_slug=channel,
            )
        resolved_slots.append(
            ResolvedSlot(slot=slot, candidates=survivors, excluded=excluded)
        )

    return Resolution(
        kit=kit,
        vehicle=fitment_pairs or "",
        slots=resolved_slots,
        engine_calls=engine_calls,
        channel_slug=channel,
    )
