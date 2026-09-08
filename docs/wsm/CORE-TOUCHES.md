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

Monkey patches on this branch: **one** (MP1 below, added by U3; the design doc
budgeted zero, see that entry for why the stock levers do not exist). Core table
edits: **zero.** Our tables carry FKs into core tables; core migrations are
untouched.

Every patch any branch of this fork installs is named, once, in
`saleor/wsm/patches.py` as `PINNED`, by the defining module and qualname of the
function it wraps. That tuple also carries MP2, the order-level no-stacking
patch on `wsm/bakeoff-voucher`, so the two branches merge without the guard
going red; a branch that carries only some of them still passes.

Both counts are assertions, not claims.
`saleor/wsm/tests/test_core_tables_untouched.py` discovers the patches actually
installed in the running process by sweeping `sys.modules` for a wrapper whose
code lives under `saleor/wsm/`, and fails on any that `PINNED` does not name. It
also walks every fork migration's DATABASE operations for a non-`wsm_` table,
including the list and `(sql, params)` forms of `RunSQL` and both directions,
and refuses a `RunPython` in a fork migration unless the file carries a
`# WSM-CORE-SAFE: <reason>` line directly above it. A patch nobody wrote down,
or a migration reaching core through the ORM, reddens the suite before it
reaches this document.

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
| `MIGRATION_MODULES = {"django_auth": "saleor.wsm.compose.django_auth_migrations"}` | `saleor.auth` already owns the `auth` label and holds the 13 historical auth migrations | duplicate migration history, `migrate` fails |

Why a relabelled `django.contrib.auth` rather than the stock one: `saleor.auth`
is a models-free shim occupying the `auth` label whose `0013` deletes Group,
Permission and User from migration state. Two apps cannot share a label, so the
one we add takes `django_auth`. Its `AppConfig.ready()` is a no-op so that
Django's `create_permissions` receiver is never connected: `wsm_compose`
permission rows are created by our own `post_migrate` receiver, into
`saleor.permission.models.Permission`.

`MIGRATION_MODULES` points `django_auth` at a FORK-OWNED migration package,
`saleor.wsm.compose.django_auth_migrations`, not at `None`. It holds two
migrations, `0001_initial` and `0002_auth_models_state_only`, and both are
state-only: they create no table and touch no row, they exist so that
`makemigrations --check` sees a history for an app that has three models in the
registry and none of its own tables. `None` was the shape U2 first wrote and the
shape this table used to claim; the code has said otherwise since
`0002_auth_models_state_only` landed on the merge branch.

Read the `AUTHENTICATION_BACKENDS` row as the app-wide change it is. A backend in
that list is consulted on EVERY `authenticate()` call in the process, not only on
/admin/, so this one password backend is part of the shop's whole sign-in path.
Since review wave A it therefore honours the merchant's own
`SiteSettings.password_login_mode`: `DISABLED` refuses everyone and
`CUSTOMERS_ONLY` refuses staff, which is what the same switch means to Saleor's
own backends (`saleor/wsm/compose/auth.py`, and its tests in
`saleor/wsm/compose/tests/test_auth.py`).

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

Monkey patches added by the merge: **zero.** MP1 below is still the only one.

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

## 7. `saleor/settings.py`, +5 lines, 1 of them code (review wave A, 2026-09-08)

```python
WSM_STOREFRONT_KEY = os.environ.get("WSM_STOREFRONT_KEY", "")
```

The per-tenant secret the storefront SERVER already sends on every call to the
fork's checkout endpoints, as `X-Dealer-Pricing-Key` (dealer) or `X-Compose-Key`
(compose). It has to be a setting because it is per-deployment configuration, and
core is where Saleor reads env into settings; everything that uses it lives in
`saleor/wsm/http.py`.

Until wave A those headers were read by nobody, on the reasoning that the price
is computed in this process so there is "nothing a caller could forge". The price
was never the forgeable thing: the BUYER was. `customerId` arrives in the request
body, so anyone on the network could read a named dealer's whole price ladder and
add lines to a checkout at that dealer's tier. The five write and price endpoints
now demand the key; the public catalog read (`.../option-sets`) does not.

Unset or empty fails SAFE: every gated endpoint answers 401. A tenant that forgot
to set the secret sells nothing through these routes, which is loud, rather than
selling at anyone's dealer price, which is silent.

Removal cost: delete the line, the decorator and `saleor/wsm/http.py`.

---

# Monkey patches

Expected: zero. Actual: **one**, in U3. Every entry names the exact function it
replaces and the upstream change that would delete it.

## MP1. Dealer lines are excluded from checkout line discounts

Installed by `saleor/wsm/dealer/apps.py` `DealerConfig.ready()`; the whole patch
is `saleor/wsm/dealer/no_stacking.py`. Two functions are wrapped, neither is
reimplemented: each wrapper calls the original with a smaller list of lines.

| Replaced function | Module | What the wrapper does |
|---|---|---|
| `attach_voucher_to_line_info(voucher_info, lines_info)` | `saleor/discount/utils/voucher.py` | Runs the original, then clears `voucher` and `voucher_code` from any line info whose line carries the `wsm.dealer` metadata key. |
| `prepare_checkout_line_discount_objects_for_catalogue_promotions(lines_info)` | `saleor/discount/utils/checkout.py` | Calls the original with the retail lines only, and adds any catalogue discount already sitting on a dealer line to the returned removal list. |

The voucher function is rebound in every module that holds it as its own
attribute: the module that defines it, plus `saleor.order.fetch` and
`saleor.graphql.checkout.dataloaders.checkout_infos`, which imported it by name
at import time. Patching the definer alone would leave those two on the original.
`saleor.checkout.fetch` imports it INSIDE the function that calls it, so it
resolves through the definer at call time and there is nothing there to rebind.

That list used to be hardcoded, and a site it did not name was skipped in
silence. Since review wave A `install` imports the pinned modules, DISCOVERS
every loaded `saleor.` module bound to the original, and raises
`ImproperlyConfigured` if the discovered set differs from the pin. An upstream
bump that adds an import site is now a boot failure with the module name in it,
not a checkout that quietly stacks a voucher onto a dealer price.

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

Known limit, deliberate: this covers LINE-level discounts (catalogue promotions,
`SPECIFIC_PRODUCT` and `apply_once_per_order` vouchers). An ENTIRE_ORDER voucher
is a checkout-level discount in Saleor and still reduces the order total that a
dealer line contributes to. Filed rather than fixed here because the fix belongs
with the order-level discount distribution, which U4's kit money spec also
touches.
