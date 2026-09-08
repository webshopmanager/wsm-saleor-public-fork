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
- U6 Import: fub option sets and dealer tiers through the migration tool stage into the new tables, census parity (111 sets, 607 values, QSST 2,443.00 exact).
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
