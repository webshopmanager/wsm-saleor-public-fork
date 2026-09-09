# Bake-off design: owned Saleor 3.23.31 with Compose + Dealer Pricing in core

Status: design pass (marshal Part 1), 2026-09-08. Author: Dana's search session. Not yet stamped by Dana.
Frame for Bill: 5.0 behavior -> this build. Never references the interim apps by name in anything he reads.

## 1. Requirements (owner: Dana, 09-08 chat; Bill, 09-08 huddle)

Problem: 39 of 47 go-live tenants are blocked on option sets, dealer pricing or series (flight measurement 09-08).
Today those live in three out-of-process services, each with its own signing, gate and reprice loop, ~13,000 lines.
Bill's plan (huddle 09-08): bring Saleor to the current version, accept modifications, sub-apps and plugins where
they fit, monkey patches for the tricky parts, full fork only if the patch list grows unmanageable, never edit core
tables, Compose and Dealer Pricing INTO core, Search and Fitment stay external behind GraphQL.

Acceptance bar (each checkable on the live bake-off box):
- B1 A non-staff merchant creates an option set with two credit values and a dealer tier row through the admin UI.
- B2 Stage 2 fixture (L600084): retail configured unit = 3494.00; Stage 3 (L600088) = 5894.01; a configured unit at or below zero is refused.
- B3 A dealer customer sees the tier price on PLP and PDP, gets it on the line, and a voucher does not stack on that line (toggle default off; toggle on = stacks).
- B4 A dealer on a container kit pays better-of per member line, never both.
- B5 Series = Collection with a side table; collection page renders members; engine never emits a series URL.
- B6 wsm-storefront develop renders PDP, collection, cart and checkout against the bake-off box with ONLY a tenant block change. No storefront code.
- B7 Per-request work is counted and stays inside the budget in section 5.
- B8 Every core touch is listed in CORE-TOUCHES.md in the branch, with its reason. Zero core table edits.

## 2. Delete the part

Disappears when pricing is in-process (each had a live consumer only because pricing was out of process):
- wsm.dealer stamp + compose.fee.sig/base/qty/chk, and the shared payment gate that re-derives them.
- X-Compose-Key, X-Dealer-Pricing-Key, X-Saleor-Domain headers and the COMPOSE_KEYS env map.
- The reprice-on-sign-in and reprice-on-quantity-change loop (price is recomputed on every fetch).
- The Go Compose service, the dealer_pricing external app, their manifests, installs, and hourly link cron.
Stays (has a live consumer): the storefront REST contract (5 endpoints) for the bake-off only, so B6 holds with zero storefront code;
line metadata wsm.options, wsm.options.sku, wsm.options.cid, wsm.options.acc, wsm.dealer (presence only), compose.fee (label, apply_to), compose.parent_line.
The product metafield compose.configurable (the PDP asks nothing without it).
Written and cleared by wsm.compose itself, on every OptionSet and Fee save and delete, fee-only products included;
the 5.0 importer is no longer the only writer.

## 3. Simplify: the seam

Saleor's price plugin hooks are the wrong seam: the unit-price hook has no caller, and the line-total hooks only run under the
TAX_APP strategy (never under flat rates). The reliable native primitive is CheckoutLine.price_override: stock column, survives
every recalculation, flows into the order. Our mutation path computes the price server-side and sets price_override itself,
so the app-only permission check on client-supplied prices is never involved (clients never send a price).

Package layout (all under saleor/wsm/, our tables only, FKs to core allowed, core tables untouched):
- wsm.compose: OptionSet, OptionValue (signed price_delta, sku_fragment, image_url, required, prompt_type), Fee (basis, amount, apply_to, required, decline_label), ProductOptionSet (product FK, order); pricing = port of internal/compose/pricing/pricing.go semantics (int cents, base + sum of deltas, zero refusal, above-retail guard on positive deltas only, tier rows on credits verbatim); REST views for endpoints 1 and 2; Django admin.
- wsm.dealer: DealerCustomer (user FK, tier group, tax_exempt bool wired to the native Checkout.tax_exemption, see the tax-exemption subsection below), TierPrice (variant FK, group, min_qty, amount), TierOptionPrice (option value FK, group, amount), Settings row (discount_stacking bool default False); REST views for endpoints 3, 4, 5; Django admin.
- wsm.containers: SeriesConfig (collection OneToOne, axes JSON, member rules), KitConfig (collection OneToOne, discount, proration = list price, bundle_kit_group_id on lines); better-of rule from 2.3 of the requirements doc.
Core touches expected (counted, each one line): INSTALLED_APPS += 3 apps and django.contrib.admin; urls.py mounts /wsm/ and /admin/. Monkey patches expected: zero. Any that appears is logged in CORE-TOUCHES.md with the upstream issue it would take to remove it.

## 4. Accelerate: units, one agent each, in order

- U0 Saleor 3.23.31 on wsm-devops :8020, Postgres 16 local, systemd. (running)
- U1 wsm.compose models + pricing + tests (fixture table from REQUIREMENTS-configured-pricing 1.2 as the test cases; prove the summation test fails when summation is replaced).
- U2 wsm.compose REST endpoints 1 and 2 with price_override + metadata write; admin registered; B1 and B2 walked as a non-staff merchant with screenshots.
- U3 wsm.dealer models, endpoints 3 to 5, no-stacking toggle, B3 walked.
- U4 wsm.containers, B4 and B5 walked.
- U5 storefront develop pointed at the box (tenant block only), full shopper walk PDP -> cart -> checkout, screenshots, per-request query count (B6, B7).
- U6 Import: fub option sets and dealer tiers out of 5.0 into the new tables, census parity (111 sets, 607 values, QSST 2,943.00 exact).
- U7 Rebase check: Bill's Saleor patches re-applied onto 3.23.31 (conflict surface from the patch inventory), then Tonneau Outlaw data on the stack.

## 5. Budget (design for the millionth run)

- PDP read: 1 query for sets+values+fees (prefetch), served with a 300 s revalidate as today.
- Configured add: 1 pricing computation, 1 checkout line write, 0 webhooks, 0 network hops, 0 signatures.
- Dealer display batch: 1 query for up to 100 variants.
- Checkout recalculation: 0 extra queries from us (price_override is already on the line).
- Processes added to the box: 0 (everything runs inside the Saleor worker).

## 6. Open, with defaults

- Storefront transport for the bake-off: REST as today (default), GraphQL later when the storefront moves. Bill's GraphQL-as-aggregator rule is for external features.
- Admin UI: Django admin (default, Bill's framework pattern). Dashboard app extension later if merchants need it inside Saleor Dashboard.
- Tenant toggle scope: per Saleor instance (one tenant per instance today).
- Kit freight class rollup and Shopify/BC collection import stay open (requirements doc section 5).

## 7. The merchant admin (added 2026-09-08 after the merchant walk)

A merchant walk of the screens as a non-superuser staff user found the admin
saving prices that break add-to-cart and reporting success. "Ready" means a
merchant can operate it live, so the rules below are on the MODEL, not on a
form: the admin, the value inline and any writer that validates get one answer
from one place. There is no REST write path for these rows today
(`saleor/wsm/compose/views.py` is read plus checkout), so the admin is the only
caller; a future one inherits the rules by calling `full_clean()`.

### The rules, in the merchant's words

- **A configured price can never reach zero.** `OptionValue.clean()` and
  `OptionSet.clean()` compute the product's FLOOR: its cheapest listed variant
  price, plus, for every question, the cheapest legal answer (a required
  one-of contributes its lowest delta, a many-of contributes every credit it
  carries, an optional one-of contributes its lowest delta or nothing). The
  floor has to stay above zero, because `pricing.price_configured` refuses at
  or below it (requirement 1.3). The floor is computed once for retail and once
  for EVERY dealer group with a tier row on the product, pricing each choice the
  way `delta_for` would price it for that group: the retail floor is only an
  upper bound on a dealer's, and a group whose credits are deeper goes under
  first. A product with no priced listing is not checked at all.
- **A SKU code is unique inside one question.** Two choices with the same
  `sku_fragment` produce one composite SKU at two different prices, and the
  order cannot say which was bought. Enforced in `clean()` and NOT as a UNIQUE
  index: Fuel Lab's live catalog carries eight colliding pairs today (the four
  QSST surge-tank pump questions, where `49614` and `494xx` each name both a
  dual and a triple pump, sixteen rows), and a database constraint would refuse
  the 5.0 import that produced them. Upgrade path: correct those source rows,
  then add `UniqueConstraint(option_set, sku_fragment)` under a condition of
  `~Q(sku_fragment="")`.
- **A dealer group has to exist.** `tier_group` was free text, and a typo saved
  a row that priced nothing and said it worked. `validate_tier_group_code()`
  checks it against `DealerGroup.code`, and the admin renders it as a dropdown
  of the codes that exist. ponytail: wsm.dealer owns that namespace and is
  growing its own validator; the swap is deleting two functions in
  `compose/models.py` and importing that one.
- **A charge is money the shopper owes.** `Fee.clean()` refuses a negative
  amount (a reduction belongs on an option choice as a credit, where the floor
  rule guards it) and a percentage above 100. No override flag.

### Money rules added after the Wild West review (2026-09-08)

Nine reviewers walked the fork and every money defect below was reproduced
before it was fixed. Each carries what it let through.

- **One rounding rule for the fork, `saleor/wsm/money.py`, HALF_UP.** The same
  conversion was written three times in two modes: `compose.models.to_cents`
  quantized with the Decimal default, HALF_EVEN, while the dealer ladders and
  the kit math rounded HALF_UP. A tier price of 8.005, which the field's three
  decimals invite, was quoted to the shopper at 8.01 by the dealer endpoint and
  charged at 8.00 by anything that priced through Compose: the shop's own two
  surfaces disagreed by a cent on the same row, every time, in whichever
  direction the merchant did not expect. Both names still exist and both now
  point at the one function, so no caller changed. HALF_UP is the rule because
  it is what the ladders already quote and what a merchant means by half a cent;
  Saleor's own `quantize_price` stays HALF_EVEN and is untouched.
- **A dealer tier price is at least one cent.** `TierPrice.amount` is an
  absolute price, not a discount, so a zero or negative one is not a deep
  discount but a line that pays the shopper. Nothing checked it: on the bake-off
  box a row of -50 priced a real checkout line at a unit price of -50.00, and a
  dealer could have ordered a cart that owed him money. Three statements of one
  rule now: a `MinValueValidator` on the field for the merchant, a
  `CheckConstraint` on our own `wsm_dealer_tierprice` table for the importer and
  the shell, and a floor in `ladders()`, which is the single read every dealer
  price comes out of, for the rows written before either existed. The floor is a
  cent rather than "above zero" because the amount carries three decimals and is
  charged at two: 0.004 is a positive number and a zero charge.
- **An option-value tier delta is bounded on both sides.** Above, a tier row
  higher than the retail delta (floored at zero) is refused at save, in the
  field, naming the retail amount. It used to save clean, quote retail on the
  product page and then refuse the add-to-cart from `pricing.delta_for` with a
  message written for a developer, so a merchant who priced a free option at
  25.00 for one dealer group broke that group's cart and got told nothing.
  Below, the charge is the BETTER OF the tier delta and retail: a -300.00 dealer
  credit sitting where retail credits -445.00 is a legal row and used to stand
  verbatim, which the merchant walk measured as a dealer quoted 3,698.99 for the
  configuration a walk-in shopper buys at 3,553.99. A dealer never pays more
  than retail for the same choice, and a line that lost to retail is not stamped
  as tier-priced, so promotions still reach it.
- **A kit member holds at least one of its variant, and the discount reported is
  the discount taken.** `KitMember.quantity` of 0 divided by zero inside the
  proration and surfaced as a 500 rather than as the bad kit row it is. And a
  fixed discount smaller than the member quantities can carry (two 1.00 units,
  one cent off) allocated nothing while `discount_cents` still reported the ask,
  so the kit's own arithmetic disagreed with itself by a cent: an order that
  said it discounted money it had charged. The residue is not invented onto a
  unit price it cannot divide into; `price_kit` reports what the members
  actually took, and `list_total - discount == total` holds again.
- **The 5.0 importer validates every row it writes.** Twelve write sites went
  straight to `save()`, so every rule above was bypassed by the one writer that
  touches a whole tenant at once: sub-zero configurations, dealer deltas above
  retail and tier groups naming no dealer group all landed, and the MERCHANT met
  the refusal weeks later on a screen that would not save until they fixed a row
  they had never written. Each write now runs `full_clean()` first, and a
  refused row is a reported skip carrying its reason, never a crash that costs
  the tenant the import and never a silent write.

- **A fee line is never discountable.** A voucher, an order promotion and a
  catalogue promotion all reach every line in the cart, and a fee is a line. The
  live re-walk measured a 100.00 order voucher on a dealer-priced configured item
  plus its required crate charge: the dealer line was protected, so the entire
  100.00 landed on `CRATE-01` and it billed at 49.00 instead of 149.00, and a
  larger code would have zeroed a mandatory pass-through charge. Retail carts were
  open more widely still, since nothing protected either line. 5.0 is the rule
  restored: a coupon comes off the merchandise subtotal and never off a product
  fee, because crating, core and environmental charges are money the merchant
  passes through rather than margin to give away. One function, MP1's
  `split_discountable`, decides it for both halves of the no-stacking patch, from
  the private `compose.fee` stamp the line already carries, required charge and
  optional one alike. Where the cart holds nothing else discountable, the code is
  accepted and takes 0.00, which is right on the money and thin for the shopper:
  the "merchandise only" message belongs on the storefront.

Where one submit changes several rows at once, the value inline formset
(`compose/forms.py`) runs both cross-row rules over the whole POST and the rows
carry `floor_checked_by_formset` so the model skips its single-row version
there. A row checked against its STORED siblings would refuse the very submit
that fixes them.

### Dealer tax exemption (Dana, 2026-09-09)

`DealerCustomer.tax_exempt` is WIRED, not hidden: it writes Saleor's own
`Checkout.tax_exemption`, the flag `saleor/checkout/calculations.py` reads to
skip tax and `complete_checkout.py` copies onto the order. 5.0 spelled the rule
as one checkbox on the dealer account and this fork spells it the same way, so
a dealer flagged exempt pays no tax at checkout and nothing else changes. We
add no table, no migration and no tax arithmetic of our own: one boolean is
copied from the account onto the cart by `saleor/wsm/dealer/tax.py` and stock
Saleor does the rest, which is what puts an exempt order on the same audit
surface as one a staff member exempted by hand. The flag is written on all
three key-gated routes that price a buyer, the dealer line (4), the dealer
reprice (5), the configured line (2) and the kit line, because the exemption is
a fact about the BUYER and not about the shape of what they put in the cart: a
Fuel Lab order is configurations end to end and would otherwise be taxed in
full. Writing it expires the checkout's prices exactly as `taxExemptionManage`
does, since `tax_exemption` is a pricing input and a cart that changed it
without expiring keeps quoting tax it no longer owes. No new route was needed.

The exploit, and why it does not work. `customerId` arrives in a REQUEST BODY,
so tax-free shopping would be one forged body away if the routes that read it
were open. They are not: every one of them is behind the tenant's storefront
key (`saleor/wsm/http.py`), which the storefront SERVER holds and a browser
never sees, and which fails safe when unset. The second-guess forgery is the
line stamp: `saleor/wsm/reprice.py` re-derives an anonymous checkout's dealer
PRICE from the group name stamped in a line's private metadata, so a forger
might expect the exemption to follow the same stamp. It does not, and it never
reads one: the flag has exactly two writers, staff holding MANAGE_TAXES through
`taxExemptionManage`, and the key-gated routes reading our own
`DealerCustomer` row. An anonymous cart carrying a dealer stamp gets the dealer
price and pays tax, which
`test_an_anonymous_cart_carrying_a_dealer_stamp_is_still_charged_tax` proves in
the same run that proves the stamp was honoured for the price. No checkout
mutation takes `taxExemption` as an input, so a shopper cannot hand it to
themselves either, and that too is asserted rather than assumed
(`test_a_shopper_cannot_hand_themselves_the_exemption_through_the_api`), so a
rebase that loosened the permission reddens this suite.

One deliberate asymmetry: a shopper with NO dealer account is left exactly as
they were rather than written False. Staff exempt a one-off buyer through
`taxExemptionManage` and that buyer carries on shopping, so writing False for
everyone we do not recognise would undo a merchant's decision silently, for
money, on the next add to cart. A DEALER's flag is written every call, True or
False, so an exemption a merchant revokes is off the cart on the next call
rather than at the next cart the shopper happens to start.

### The 422 a shopper reads

`NegativeTotalError` used to say "configured price is -935000 cents; refusing to
create the line". It now says "This configuration comes to -9,350.00, which is
not a valid price. Please contact us." Currency units, no currency code:
`pricing.py` has no channel and threading one through would buy a symbol at the
cost of a parameter. With the rules above it should be unreachable through the
admin.

### Static files: collect before you serve

`STATIC_ROOT` is `<repo>/static`, `DEBUG` is off, there is no web server in
front of uvicorn and whitenoise is not a Saleor dependency, so an uncollected
tree renders every admin screen unstyled with each asset a 404. Any script that
serves this app runs, after loading the environment and before exec'ing
uvicorn:

    /home/ubuntu/bakeoff/venv/bin/python manage.py collectstatic --noinput --verbosity 0

That line is now in `/home/ubuntu/bakeoff/serve.sh`, which is the `ExecStart` of
the `bakeoff-saleor` service on :8020, and in `serve-hard4.sh`. **The service on
:8020 needs one restart to pick it up**; it was deliberately not rolled by the
unit that made this change. `/static/` is already in `.gitignore`. The other
`serve-*.sh` helpers on the box each want the same line.

### The lists

`OptionSet` lists product first, then a question name truncated at 60
characters (Fuel Lab's imported names wrapped six lines deep and put four rows
on a screen that should hold thirty), the number of choices from one annotated
query, and searches by product name and variant SKU. Every field on
`OptionSet`, `OptionValue`, `DealerTierOptionPrice` and `Fee` carries help text.
`Fee.variant` is off the form entirely and read-only in a collapsed Internal
section for support.

The wording of a stored enum lives on the model field's `choices`
(`PROMPT_LABELS`, `BASIS_LABELS`, `SCOPE_LABELS` in `compose/models.py`), not on
a form. A merchant reads "Pick one", "A flat amount of money" and "Per item"
wherever the value is rendered, including the changelist FILTER, which reads the
field and never sees a form. The stored value is unchanged and is still what the
storefront and `pricing.py` speak; only the wording is new, so the migration is
`AlterField` on help text and choices and touches no data.

### The admin is a writer surface, said once

Saleor routes reads to a replica and `restrict_writer_middleware` raises on any
query that reaches the writer without asking. Django's admin predates that idea:
it reads the session and the permission rows off the default connection and
writes a `LogEntry` on every save, so with the middleware on, every merchant
screen answers 500. The box only stays up because
`ENABLE_RESTRICT_WRITER_MIDDLEWARE` is off, which makes this a production
trapdoor rather than a fixed bug.

`ComposeAdminSite.get_urls()` wraps every view under the site in `allow_writer`,
recursively, because each ModelAdmin arrives as a nested resolver and `login` is
mounted without `admin_view`, and login is the first screen a merchant hits. The
wrapper also renders the `TemplateResponse` inside the block: the admin returns
its response lazily and Django renders it after the view has returned, so the
index's recent-actions query would otherwise land outside the permission. Saying
it once at the site is what keeps the next ModelAdmin registered here from
having to know. `tests/test_admin.py` renders the index and a changelist with
the middleware on, and asserts the middleware is on before it believes either.

Not fixed here, and reported as a follow-up: one logical axis appears once per
product rather than once per catalog, which is the shape the 5.0 import
produces (97 products carry 126 questions, four of them the same QSST pump
question repeated).

### The second merchant walk (2026-09-09): six frictions, admin layer only

A non-superuser walk of the same screens, this time on Fuel Lab's real data,
found six places where the screen was operable but not usable. Every fix is in
`admin.py` and one template: no model changed, no migration, no stored data
rewritten.

- **A dealer group now says what it prices.** The form was two text fields, and
  "Dealer 1" owns 304 tier prices it said nothing about. It reports the count,
  the lowest and the highest, and links to those rows pre-filtered. A read-only
  summary, not a 304 row inline: two queries on the change page, none on the
  list.
- **A question shows what its choices cost dealers.** The choices inline carries
  a read-only column reading "Dealer 1 -50.00 USD, Dealer 2 -75.00 USD", by
  group NAME, each row linking to the choice that owns those prices. One
  prefetch and one correlated currency column for the page, plus a
  `select_related` that also pays off the two queries a row the inline was
  already spending on each choice's string. Pinned: rendering one choice and
  four costs the same number of queries.
- **The shopper-facing help field says what its markup does.** 118 of Fuel
  Lab's 126 questions carry HTML there. It is not rewritten and not sanitised:
  the storefront renders it and the merchant meant it. One sentence of help
  says so. On the list side there is nothing to strip: markup lives in the note
  alone and no column shows it, and a test now fails the day one does.
- **A series picks its questions from the store's own attributes.** `axes` was
  a free-text box of slugs, so building a series meant leaving for the Saleor
  dashboard to find out what to type. It is a checkbox list of the store's
  product attributes, by name with the slug alongside, writing back the same
  JSON list; the partitioning axis picks from the same list. A slug that
  outlives its attribute is offered anyway, marked `(missing)`, so it is never
  silently dropped. The screen also reports how many products the collection
  holds and how many are published, because publishing is refused under two
  published members and that count was only findable elsewhere. ponytail: the
  ask order is the list order, alphabetical, with no way to reorder; the
  upgrade path is an ordered widget on that one field.
- **The pickers offer what the merchant typed first.** Typing the part number
  `71801` returned the product carrying it fifth, behind covers whose SKUs
  merely contain those digits. Three tiers in one CASE (the whole term as a
  SKU, the whole term as a word in a SKU or a name, then the previous order),
  as correlated subqueries rather than joins, because joining a reverse
  relation returns a product once per variant. Nothing is filtered, only
  ordered. Same mixin on the variant picker.
- **An empty list says what the thing is.** Fees and Kits showed a heading, a
  search box, a filter sidebar and "0 fees". They now show two sentences in
  merchant words and one Add link, and hide what there is nothing to narrow. A
  search that matched nothing is a different state with a sentence Django
  already writes, and it is left alone.

## 8. Fee products are catalog plumbing (added 2026-09-08 after the SEO walk)

A fee reaches the order as its own checkout line, with its own SKU and label,
because that is what 5.0 did and what the ERP export reads. A line needs a
variant, so the first shopper who buys a fee mints one: product type `wsm-fee`,
one product at slug `wsm-fee-<pk>`, one variant, one channel listing at zero.
The listing is published and available for purchase because
`add_variants_to_checkout` refuses an unpublished variant, so the existence of
the page is load-bearing and cannot be switched off from this side.

Both channel listings carry `discounted_price_amount`, not only `price_amount`.
Stock `get_variant_availability` guards a NULL `price` and then dereferences
`discounted_price` unguarded, so a variant listing with one of the two set
answered `variants { pricing }` with a 500 to any anonymous caller, on every fee
product, and broke the ordinary `lines { variant { pricing } }` cart query for
exactly the checkouts that carry a fee. Every fee variant listing ever minted is
in that state, because the writer had no other branch: 3 rows on the box on
09-08 (2 in saleor_bakeoff, 1 in saleor_bakeoff_import; saleor_bakeoff_fub holds
no fee product at present and saleor_bakeoff_merge none either).
`_ensure_fee_variant` repairs a NULL in place the next time it runs for that fee
and channel, so the integrator repairs them by exercising the fee path once per
fee and channel, not by running a command.

### The consumer contract, in one sentence

Fee products are catalog plumbing: the storefront route 404s product type
`wsm-fee`, the sitemap excludes it, the Merchant Center feed excludes it, and the
PartsLogic indexer skips it.

`productType.slug == "wsm-fee"` is the key. It is unique, it already exists, and
it costs no new field. `visible_in_listings=False` stays, which is what already
keeps fees out of `products()`, out of search and off category pages, and the
type is `has_variants=False` and not shipping required (a `Fee` carries no
freight marker of its own; `freight_class` lives on `KitConfig`, on the kit).

A fee page fetched by its exact slug still resolves through the API by design.
The job of the API is to answer for a row that exists; deciding that a URL should
not be served belongs to the storefront. This is a storefront-lane follow-up in
wsm-storefront and the indexer, not a change in this fork.

## 9. Series: one editor, one derived blob (Dana, 2026-09-09)

The review (verdict item 10) read `SeriesConfig` plus its `/admin/` screen and
the Collection's `wsm.series` metadata as two authorities over the same fact.
Dana ruled the opposite way round from deleting the side table: **the
`SeriesConfig` screen stays as the one series editor, and the blob is strictly
derived output.**

GPT's finding 7 is correct that an ordinary side-table save overwrites a newer
hand edit of the blob, and that is now the design rather than a defect: the row
is the authority, so the stamp is unconditional and never merges with, diffs
against, or reads back what the key held. Nothing hand-edits the blob, so
nothing it could clobber has any standing.

The third door was the gap. Saving restamped and `update`, `bulk_update` and
`bulk_create` restamped, but deleting a `SeriesConfig` left the published blob
on the Collection, so a series a merchant had removed kept rendering on the
storefront and kept being indexed. All three delete doors now clear it in the
same transaction as the delete: `SeriesConfig.delete()`, a queryset `delete()`,
and the admin's `delete_selected` bulk action, which goes through
`ModelAdmin.delete_queryset` and so never calls the model's own method. Deleting
the Collection needs nothing: the row cascades and the metadata goes with the
row's own collection.

**Reader contract, unchanged and sufficient:** absent key means no series, and
that is what the clear produces. `delete_value_from_metadata` removes the key
rather than blanking it, and the tests assert `SERIES_METADATA_KEY not in
collection.metadata`, not that the blob is empty or falsy, because an empty blob
would be a series that exists and answers nothing. Readers already default a
missing `published` to hidden, so no reader needs a change.

Queries added: one metadata write per collection on a delete, on an admin path
that no shopper reaches.

## 10. Price floor stamp (Dana, 2026-09-09)

Dana ruled that a configurable product may keep a **$0 base price** for the bake-off. That takes the base price out of service as
a number anyone can be quoted, and three consumers need one: Google Merchant Center **suspends a feed on a zero price**, a PLP tile
with no number is not a tile anyone clicks, and the search engine has nothing to sort or facet on. So the product carries the
lowest price a shopper can actually pay, stamped at write time.

**The key.** `wsm.price_floor`, PUBLIC metadata on the Product, one JSON object keyed by channel slug:

```json
{"default-channel": {"amount": "493.24", "currency": "USD"}, "c-pln": {"amount": "1920.00", "currency": "PLN"}}
```

The value is stored as a JSON **string**, not a nested object, because GraphQL types `MetadataItem.value` as `String`: a dict
reaches a consumer as a Python repr with single quotes, which no JSON parser reads. `wsm.series` on a Collection is stamped the
same way for the same reason. Amounts are two-decimal strings computed in integer cents, never floats, because 493.24 as a float
is 493.2399999999998 and every consumer here is quoting money.

Per channel because a base price is per channel: a product listed in four channels has four floors, and a "from" price quoted in
the wrong currency is worse than no price. Retail only; dealer tier deltas are private to the dealer path and are never in a
public stamp.

**The formula.** The channel's base price (the cheapest `ProductVariantChannelListing.price_amount` on the product in that
channel), plus for every REQUIRED option set the cheapest legal answer, which a negative adder makes a subtraction, plus every
REQUIRED fee. Optional sets and declinable fees add nothing, for the same reason: the shopper can say no, so they are not part of
the lowest price anyone pays. A percentage fee computes on the cheapest configured subtotal, not on the base, through the same
`_apply_fees` that checkout charges through. One function, `pricing.minimum_line_cents`, wrapping the existing
`minimum_configured_cents`; there is no second formula anywhere, so the quoted floor and the charged line cannot round apart.
Quantity one, where a per-unit fee and a per-line fee are the same money.

A floor that computes below zero stamps `"0.00"` and logs at WARNING with the product id. The admin refuses to save rows that do
this and `pricing.delta_for` refuses to charge them, so reaching it means rows written around both; a negative "from" price is a
feed rejection and a broken tile, so it is clamped and said out loud rather than published.

A product with no configuration at all, or whose only Compose row is a DECLINABLE charge, gets **no key**. Its floor is its base
price and the listing already carries that, so a stamp would be a second copy of a number to go stale. Removal takes the key off
rather than blanking it: absent is what a reader reads as "no floor", where an empty object is a floor that answers nothing.

**Who writes it.** `sync_product_stamps` in `saleor/wsm/compose/models.py`, which is the `compose.configurable` maintainer
extended to keep both marks. One function and one metadata write for both, because every door that can move one can move the
other. It fires from `post_save` and `post_delete` on `OptionSet`, `Fee` and `OptionValue`. `OptionValue` is new to this hook and
is there for the floor alone: a value cannot change whether a product is configurable, but the cheapest answer to a required
question IS the floor. `DealerTierOptionPrice` is deliberately not hooked, because the stamp is retail only. The 5.0 importer
stamps through the same function in its own final pass, so an imported catalog and a hand-built one can never carry a different
answer.

**Cost.** Six queries per affected product per merchant save: the sets, their values, the fees, the product row, the per-channel
base prices (one grouped query, not one per channel), and one `UPDATE` of `metadata`. Nothing at all when the answer has not
changed, which is what makes a re-run free. **Zero queries on any shopper path**, which is the whole point: index-time
denormalization, never a read cache. A `Fee` row has a single product FK, so a fee touches exactly one product and there is no
fan-out to batch.

**Known gap: the base price.** Nothing stamps on a base-price change. That price lives on `ProductVariantChannelListing`, a core
table Saleor writes through `bulk_update` on the discounted-price path, which fires no signal at all; catching it would mean
either a broad signal on a hot core write or editing core, and neither is worth it for a number a merchant changes by hand a few
times a year. The answer is `manage.py wsm_stamp_price_floor [--product-ids ...]`, which restamps and is idempotent (a second run
issues no write, proved by query capture). Run it after a price change, a price import, or a channel being added. The importer
already calls the same function, so a 5.0 import needs nothing extra.

**Who reads it (follow-ups, each in its own lane, none in this unit).** The storefront PLP renders it as the "from" price on a
configurable tile; the Google Merchant Center feed sends it as `price` in place of a $0 base; the PartsLogic search engine indexes
it as the sortable and facetable price. All three read a stamp and compute nothing.

**Exploit paragraph.** The stamp is public and read-only from outside: metadata is exposed on the Product, and the only writers
are our own save paths and the management command, all of which derive it from the rows. Nothing accepts it as input. A forged or
tampered stamp costs nothing, because **checkout never reads it**: `price_configured` prices from the catalog rows every time and
refuses at or below zero, so the worst a wrong stamp can do is advertise a price the shopper is not charged, which is a
merchandising defect and not a money one. The clamp at zero exists for the same reason in reverse: a bad row should mislead a feed
as little as possible while it is being found.
