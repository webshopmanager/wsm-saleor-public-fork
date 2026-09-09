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

## 3. Simplify: the seam

Saleor's price plugin hooks are the wrong seam: the unit-price hook has no caller, and the line-total hooks only run under the
TAX_APP strategy (never under flat rates). The reliable native primitive is CheckoutLine.price_override: stock column, survives
every recalculation, flows into the order. Our mutation path computes the price server-side and sets price_override itself,
so the app-only permission check on client-supplied prices is never involved (clients never send a price).

Package layout (all under saleor/wsm/, our tables only, FKs to core allowed, core tables untouched):
- wsm.compose: OptionSet, OptionValue (signed price_delta, sku_fragment, image_url, required, prompt_type), Fee (basis, amount, apply_to, required, decline_label), ProductOptionSet (product FK, order); pricing = port of internal/compose/pricing/pricing.go semantics (int cents, base + sum of deltas, zero refusal, above-retail guard on positive deltas only, tier rows on credits verbatim); REST views for endpoints 1 and 2; Django admin.
- wsm.dealer: DealerCustomer (user FK, tier group), TierPrice (variant FK, group, min_qty, amount), TierOptionPrice (option value FK, group, amount), Settings row (discount_stacking bool default False); REST views for endpoints 3, 4, 5; Django admin.
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

Where one submit changes several rows at once, the value inline formset
(`compose/forms.py`) runs both cross-row rules over the whole POST and the rows
carry `floor_checked_by_formset` so the model skips its single-row version
there. A row checked against its STORED siblings would refuse the very submit
that fixes them.

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
