# CORE-TOUCHES

Every line this branch changes in a file Saleor owns, and why. Counted on
purpose: acceptance bar B8 of `BAKEOFF-design-2026-09-08.md` is that this list
stays short and that zero core TABLES are edited. A table in a pull-request
description is invisible six months later, when the person doing the rebase
needs it.

The complete inventory is one command, and it must return this file's list and
nothing else:

```
git diff --name-only a1ab3a2..HEAD -- saleor/   # settings.py + saleor/wsm/** only
```

Monkey patches: **two** (MP1, added by U3, and MP2, added by U7; the design doc
budgeted zero, see those entries for why the stock levers do not exist). Core
table edits: **zero.** Our tables carry FKs into
core tables; core migrations are untouched.

---

## 1. `saleor/settings.py`, +2 lines (U1, 2026-09-08)

```python
    # WSM-FORK: fork-only app, see saleor/wsm/compose/__init__.py and docs/wsm/CORE-TOUCHES.md
    "saleor.wsm.compose",
```

Registering the app in `INSTALLED_APPS` is the only thing core can do that an
app cannot do for itself: without it Django never loads the models and
`wsm_compose`'s migrations do not exist. Everything else in Compose (models,
migrations, pricing, tests) lives under `saleor/wsm/compose/`, which upstream
does not own, so a rebase sees one conflict site rather than a patch series.

The app label is set explicitly to `wsm_compose` in `apps.py`, because Django
would otherwise name it `compose` and the tables would lose the `wsm_compose_`
prefix that keeps them out of core's namespace.

Placed last among the local apps and before the external ones, matching the
placement `webshopmanager/saleor` uses for `saleor.wsm` on `develop`.

Removal cost: delete the two lines and the package. Nothing in core references
Compose.

---

## 2. `saleor/settings.py`, +33 lines, 13 of them code (U2, 2026-09-08)

The merchant UI is the Django admin (design section 6), and the admin will not
start on a Saleor that amputated `django.contrib.auth`. Every line here exists
to satisfy one of its dependencies. Grouped, with what breaks without it:

| Lines | What | Without it |
|---|---|---|
| `INSTALLED_APPS`: `saleor.wsm.compose.apps.WsmAuthConfig` | `django.contrib.auth`, relabelled `django_auth` | `admin.E403`: the admin refuses to load |
| `INSTALLED_APPS`: `django.contrib.sessions`, `django.contrib.messages`, `django.contrib.admin` | the admin itself and the two apps it requires | no admin |
| `INSTALLED_APPS`: `django.forms` | Saleor sets `FORM_RENDERER = TemplatesSetting`, which resolves widget templates through the loaders | `TemplateDoesNotExist` on every admin form |
| `MIDDLEWARE`: session, authentication, message | admin checks `admin.E40x` | admin refuses to load |
| `TEMPLATES` context processors: `auth`, `messages`, `request` | same checks, plus `admin.W411` for the sidebar | admin refuses to load |
| `AUTHENTICATION_BACKENDS`: `saleor.wsm.compose.auth.AdminPasswordBackend` | email + password login, and permissions read from Saleor's renamed `permission_permission` | no way to log in as a merchant |
| `MIGRATION_MODULES = {"django_auth": None}` | `saleor.auth` already owns the `auth` label and holds the 13 historical auth migrations | duplicate migration history, `migrate` fails |

Why a relabelled `django.contrib.auth` rather than the stock one: `saleor.auth`
is a models-free shim occupying the `auth` label whose `0013` deletes Group,
Permission and User from migration state. Two apps cannot share a label, so the
one we add takes `django_auth` and contributes no migrations of its own. Its
`AppConfig.ready()` is a no-op so that Django's `create_permissions` receiver is
never connected: `wsm_compose` permission rows are created by our own
`post_migrate` receiver, into `saleor.permission.models.Permission`.

Removal cost: delete the block. Nothing outside `saleor/wsm/` imports any of it.

## 3. `saleor/settings.py`, +2 lines (U3, 2026-09-08)

```python
    # WSM-FORK: fork-only app, see saleor/wsm/dealer/__init__.py and docs/wsm/CORE-TOUCHES.md
    "saleor.wsm.dealer",
```

Same reason as Compose above, and the same removal cost: delete the two lines
and the package. The app label is pinned to `wsm_dealer` in `apps.py` so the
tables keep their `wsm_dealer_` prefix.

## 4. `saleor/settings.py`, +2 lines (U4, 2026-09-08)

```python
    # WSM-FORK: fork-only app, see saleor/wsm/containers/__init__.py and docs/wsm/CORE-TOUCHES.md
    "saleor.wsm.containers",
```

The twin of touch 1, for the same reason: `INSTALLED_APPS` is the only thing an
app cannot do for itself. The label is pinned to `wsm_containers` in `apps.py`
so the three tables carry the `wsm_containers_` prefix and stay out of core's
namespace. Everything else in Containers (models, migration, pricing, views,
admin, tests) lives under `saleor/wsm/containers/`, which upstream does not own.

This is the SHARED touch site: U1, U3 and U4 each add their own line in the
same block. The merge kept all three, in unit order.

Removal cost: delete the two lines and the package. Nothing in core references
Containers.

---

## 5. `saleor/urls.py`, +1 line of code (U2, U3 and U4 together, 2026-09-08)

```python
    re_path(r"", include("saleor.wsm.urls")),
```

**Shared touch.** U2 (Compose endpoints, the merchant admin, static), U3
(Dealer endpoints) and U4 (the kit endpoint) each need the fork's URLs mounted, and both branches wrote
this line their own way. U2's shape is the one kept: U3's `path("wsm/", ...)`
prefixes every fork route with `/wsm/`, which the dealer and compose endpoints
want but `/admin/` and `/static/` do not, and those two are U2's merchant UI.
An empty `re_path` prefix mounts the list at the root and lets
`saleor/wsm/urls.py` spell out each app's own prefix, so all three shapes fit
behind one core line. The dealer routes keep their exact paths
(`/wsm/dealer_pricing/...`) because `saleor/wsm/urls.py` now carries the
`wsm/` segment in its own pattern.

Core gains ONE include however many fork apps exist, because
`saleor/wsm/urls.py` is the list. Adding the next app's endpoints is a line in
that file, which upstream does not own, rather than another core touch.

Removal cost: delete the line and the `saleor/wsm/` package. Nothing in core
resolves a `wsm-` route name.

---

## 6. Nothing. What the merge added, and where (U6, 2026-09-08)

Landing all three units on one branch added **zero** lines to a file Saleor
owns. Sections 1 to 5 are still the complete core-touch list, and the command at
the top of this file still returns `saleor/settings.py`, `saleor/urls.py` and
`saleor/wsm/**` and nothing else. What the merge did add lives entirely inside
`saleor/wsm/`, and is listed here because a reviewer looking for the seam should
not have to diff three branches to find it.

| File | What changed | Why it could not stay as it was |
|---|---|---|
| `saleor/wsm/containers/views.py` | `resolve_tier_lookup` returns a real lookup instead of `None` | It was written as the seam and documented as returning retail "until wsm.dealer lands". It has landed. |
| `saleor/wsm/compose/apps.py` | the receiver's label guard is a three-label set, and it is connected without a `sender` | `post_migrate` fires once per app; with `sender=self` it only ever saw `wsm_compose`, so `wsm_dealer` and `wsm_containers` got no permission rows and a merchant who is not a superuser could not be granted their screens. |
| `saleor/wsm/dealer/admin.py` | registers on the compose `AdminSite`, with `WsmAdminMixin` | `@admin.register(Model)` puts a screen on Django's default `admin.site`, which this fork does not mount: the four dealer screens existed and were unreachable. Without the mixin they also 500 for a non-superuser, because Saleor's `User` has no `has_module_perms`. |
| `saleor/wsm/containers/admin.py` | imports `WsmAdminMixin` rather than repeating it, and registers unconditionally | Its own docstring called for exactly this once both units sat on one branch. The `try: import ... except ImportError` around the registration was there for a branch that no longer exists, and a swallowed `ImportError` is how screens go missing silently. |
| `saleor/wsm/compose/django_auth_migrations/0002_auth_models_state_only.py` | new, state-only, no database operations | Pre-existing on U2 and not caused by the merge: `makemigrations --check` failed on every branch, because `django_auth` has three models in the app registry and an empty migration history. See the file's own docstring. Fixed here rather than filed, because a merge branch whose acceptance bar includes a clean `makemigrations --check` cannot hand that check on. |
| `saleor/wsm/dealer/urls.py` | the three routes carry the `api/` segment they are called with | U3 mounted them one segment short of the contract the storefront already posts to (`src/lib/dealerPricing.ts` and `src/app/api/cart/dealer-reprice/route.ts` both spell `/wsm/dealer_pricing/api/...`), and U3's own tests asserted the short paths, so the suite was green against the wrong URL. Found by requesting the live endpoint, not by reading the code: both dealer proofs came back 404 HTML. Compose and containers already spell `api/` inside their own url modules; dealer was the outlier. |
| `saleor/wsm/tests/test_core_tables_untouched.py` | new, 60 lines, no new dependency | The rule "we add tables, we never modify Saleor's" was a habit and a doc. It is now a test: half one asserts that nothing outside `saleor/wsm/` differs from the upstream tag except settings.py and urls.py, half two walks every fork migration's DATABASE operations and asserts each one names a `wsm_` table. Proven able to fail: appending a comment to `saleor/product/models.py` reddens the first (`AssertionError: ['saleor/product/models.py', 'saleor/settings.py', 'saleor/urls.py']`) and a scratch migration running `SELECT 1 FROM product_product WHERE false` reddens the second (`AssertionError: wsm_compose.0099_probe writes to product_product`). Both probes removed. |

The tier lookup reads every member's breaks in ONE query before pricing starts,
rather than one query per member from inside the loop, and makes no query at all
for a shopper who is not signed in. Better-of on a kit member is still decided by
`price_kit`, which already took a `tier_lookup`: no money rule moved.

Monkey patches added by the merge: **zero.** MP1 was the only one on the branch
at that point; MP2 arrived after it, in U7.

---

## What U4 deliberately did NOT touch

- **No GraphQL.** A series collection page is the STOCK collection page. The
  series facts (`brand`, `axes`, `partitioning_axis`, `miss_message`) are written
  onto the Collection's own metadata under the key `wsm.series` by
  `SeriesConfig.save`, through the stock metadata API, so the storefront and the
  search indexer read them with the query they already make. No new field, no
  new type, no schema snapshot to regenerate.
- **No core table.** The three tables are ours; the FKs point INTO
  `product.Collection` and `product.ProductVariant` and no core migration moves.
- **No hand-written CheckoutLine.** Kit members are added through
  `add_variants_to_checkout`, the same function `checkoutLinesAdd` calls, with
  `price_override` computed in this process. A kit is never a Saleor object
  beyond the Collection.
- **No `django.contrib.admin` registration in settings.** `saleor/wsm/containers/admin.py`
  registers onto the AdminSite that U2 mounts; on a containers-only branch there
  is nothing to register onto and the screens are exercised by calling
  `admin.register(AdminSite())` in a test. When U2 and U4 sit on one branch the
  screens appear with no further edit. The `post_migrate` permission receiver in
  `saleor/wsm/compose/apps.py` needed `wsm_containers` in its label guard so a
  non-superuser merchant is grantable on them: done in section 6 below, and it
  was the whole merge cost, as predicted.

---

# Monkey patches

Expected: zero. Actual: **two**, in U3 and U7. Every entry names the exact
function it replaces and the upstream change that would delete it.

## MP1. Dealer lines are excluded from checkout line discounts

Installed by `saleor/wsm/dealer/apps.py` `DealerConfig.ready()`; the whole patch
is `saleor/wsm/dealer/no_stacking.py`. Two functions are wrapped, neither is
reimplemented: each wrapper calls the original with a smaller list of lines.

| Replaced function | Module | What the wrapper does |
|---|---|---|
| `attach_voucher_to_line_info(voucher_info, lines_info)` | `saleor/discount/utils/voucher.py` | Runs the original, then clears `voucher` and `voucher_code` from any line info whose line carries the `wsm.dealer` metadata key. |
| `prepare_checkout_line_discount_objects_for_catalogue_promotions(lines_info)` | `saleor/discount/utils/checkout.py` | Calls the original with the retail lines only, and adds any catalogue discount already sitting on a dealer line to the returned removal list. |

The voucher function is also rebound in the three modules that imported it by
name at import time (`saleor.checkout.fetch`, `saleor.order.fetch`,
`saleor.graphql.checkout.dataloaders.checkout_infos`); patching the defining
module alone would leave them on the original.

Why (requirement 2.4, Dana's ruling 2026-09-08): "no discount combines with
dealer pricing", as a per-tenant toggle defaulting OFF.

Why a patch and not a stock lever, all three checked in the 3.23.31 source first:

- Vouchers on a checkout are not stored objects. `attach_voucher_to_line_info`
  decides which lines carry one and `calculate_base_line_total_price` subtracts
  it. The only line stock code ever excludes is a gift (`get_discounted_lines`,
  `is_gift`). There is no hook, setting or per-line flag.
- Catalogue promotions are DESIGNED to stack on top of a price override:
  `saleor/discount/utils/promotion.py` lines 202-216 read `line.price_override`
  and apply the rule to it. Nothing there is configurable.
- `manual_line_discount` does block a voucher, but only on ORDER lines, and
  buying that behaviour would mean writing a fake zero-value MANUAL discount onto
  every dealer line, which would then appear in the API, the order and the
  invoice as a discount the merchant never created.

Cost when it does nothing: zero. A checkout with no dealer line takes the
original path with no extra call and no settings query. The toggle is read only
once a dealer line is present, and cached per process.

Upstream change that deletes this file: a documented per-line discount exclusion
on the checkout path, for example a `CheckoutLine.discounts_excluded` flag or a
`can_discount_line(line_info)` predicate honoured by both
`attach_voucher_to_line_info` and
`prepare_checkout_line_discount_objects_for_catalogue_promotions`, the way
`is_gift` already is.

Scope: this covers LINE-level discounts (catalogue promotions, `SPECIFIC_PRODUCT`
and `apply_once_per_order` vouchers). ENTIRE_ORDER vouchers and order promotions
are checkout-level discounts in Saleor, they never reach a line, and MP1 leaves
them alone. U3 filed that gap rather than fixing it; MP2 below closes it.

---

## MP2. Dealer lines are excluded from ORDER-LEVEL discounts

Installed by `saleor/wsm/dealer/apps.py` `DealerConfig.ready()`, right after MP1;
the whole patch is `saleor/wsm/dealer/no_stacking_order_level.py`. Five functions
are wrapped, none is reimplemented: each wrapper calls the original with a smaller
set of lines and puts the dealer lines back at the price they already carried.

MP1 closed line-level discounts. It cannot see this one: an ENTIRE_ORDER voucher
never reaches a line, so there is no line voucher for MP1 to clear.
`attach_voucher_to_line_info` attaches a voucher only when it is `SPECIFIC_PRODUCT`
or `apply_once_per_order` (`saleor/discount/utils/voucher.py` lines 194-217). An
ENTIRE_ORDER voucher is one amount on the checkout, and every line pays a share.

| Replaced function | Module | What the wrapper does |
|---|---|---|
| `get_voucher_discount_for_checkout(manager, voucher, checkout_info, lines, address)` | `saleor/checkout/utils.py` | For an order-level voucher, calls the original with the retail lines only, so the percentage is taken of the retail subtotal. |
| `_propagate_checkout_discount_on_checkout_lines_prices(lines, total_discount, currency)` | `saleor/checkout/base_calculations.py` | Spreads over the retail lines only, then yields each dealer line at its own `calculate_base_line_total_price`. |
| `propagate_order_discount_on_order_prices(order, lines)` | `saleor/order/base_calculations.py` | Calls the original with the retail lines, so every `OrderDiscount` row is resized from the retail subtotal, and adds the dealer lines' total back into the returned subtotal. |
| `propagate_order_discount_on_order_lines_prices(lines, base_subtotal, subtotal_discount)` | `saleor/order/base_calculations.py` | Spreads over the retail lines against a base reduced by the dealer total, then yields each dealer line at its `base_order_line_total`. |
| `create_discount_objects_for_order_promotions(order_or_checkout, lines_info, subtotal, channel, country)` | `saleor/discount/utils/promotion.py` | Hands the original a subtotal with the dealer lines taken out, so an order promotion is both sized and threshold-tested on retail money. One function serves the checkout and the order, so this covers both. |

Two of the five are the discount AMOUNT and two are the SPREAD, and they have to
move together. Fixing only the spread would size a discount on the dealer line's
money and then hand all of it to the retail lines, which is worse than stock: the
merchant would give a deeper retail discount because a dealer line was in the cart.

Rebinding: `propagate_order_discount_on_order_prices` is imported by name into
`saleor.plugins.manager`, and `create_discount_objects_for_order_promotions` into
`saleor.discount.utils.checkout` and `saleor.discount.utils.order`. Those bindings
are rewritten too. The other two have a single call site each, inside their own
defining module, so a module-attribute swap is enough.

Why (requirement 2.4, Dana's ruling 2026-09-08): "no discount combines with dealer
pricing", as a per-tenant toggle defaulting OFF. This is the "known limit,
deliberate" paragraph at the end of MP1, now closed.

**What a shopper could do without it.** A checkout holding one dealer line at
3400.00 (a tier the merchant granted that account) and one retail line at 6399.00,
with a 10 percent ENTIRE_ORDER voucher. Stock Saleor sizes the discount on the
whole 9799.00, which is 979.90, and then spreads it in proportion to each line's
share of the subtotal. The dealer line's share is 3400.00 / 9799.00, so 340.00 of
that discount lands on the dealer line and it bills at 3060.00: ten percent off a
price that was already the dealer's negotiated price. The voucher code is public,
the dealer account is the one place a merchant has already given ground on margin,
and nothing in stock Saleor stops the two combining. With MP2 the discount is
639.90, the dealer line bills at 3400.00, the retail line at 5759.10, and the
checkout total is 9159.10. Measured both ways in
`saleor/wsm/dealer/tests/test_no_stacking.py`: with the install line commented out
the same tests fail on `Money('979.90') == Money('639.90')` and
`Money('3060.00') == Money('3400.00')`.

Why a patch and not a stock lever, all three candidates checked in the 3.23.31
source first:

- The two spread functions take a plain list of lines and divide by
  `share = line_total / subtotal`. They read no flag, skip no line and expose no
  hook. `is_gift`, the one line-shaped exclusion in stock discount code, lives in
  `get_discounted_lines` on the voucher side and never reaches either of them.
- `price_override` is not a special case on this path. It sets the unit price and
  is then treated like any other price: `CheckoutLineInfo.variant_discounted_price`
  returns it and the spread divides it up with the rest.
- Excluding the line upstream, by keeping it out of the `lines` list the caller
  passes down, is not available either: that same list is the subtotal, the tax
  base and the order-line source. A line dropped from it is a line nobody bills.

The plugin manager's `calculate_checkout_line_total` is the nearest thing to a
supported override, and it is not one: it only reshapes a line total that has
already been computed, it cannot change the discount AMOUNT that was sized on the
dealer line's money, it does not reach the order's own recalculation, and plugins
are on their way out in favour of tax apps. It would move the number on one screen
and leave the order wrong.

Cost when it does nothing: zero. Every wrapper's first act is a dict-key test on
lines already in memory. With no dealer line among them the original runs on the
original arguments, and no query, no settings read and no `Money` arithmetic is
added. The toggle is consulted only once a dealer line is present, and it is the
same per-process cached read MP1 uses, so a retail-only fleet pays nothing.

Upstream change that deletes this file: an exclusion honoured by the order-level
discount base, for example a `discountable` predicate on the line consulted by
`base_checkout_subtotal` and by both propagate functions, the way `is_gift` is
already consulted on the voucher side. That one predicate would delete MP1 as well.
