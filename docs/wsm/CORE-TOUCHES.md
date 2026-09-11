# CORE-TOUCHES

Every line this branch changes in a file Saleor owns, and why. Counted on
purpose: acceptance bar B8 of `BAKEOFF-design-2026-09-08.md` is that this list
stays short and that zero core TABLES are edited. A table in a pull-request
description is invisible six months later, when the person doing the rebase
needs it.

The complete inventory is one command, and it must return this file's list and
nothing else:

```
git diff --name-only a1ab3a2..HEAD -- saleor/   # settings.py, section 7's list, saleor/wsm/**
git diff --name-only a1ab3a2..HEAD -- CHANGELOG.md
```

The second line is not decoration. The first is scoped to `saleor/`, so it
cannot see `CHANGELOG.md`, which this branch also edits and which
`make vendor-delta` counts. One file, outside the only command this ledger
offered to find it (Wild West finding 12).

Core touches from outside `saleor/wsm/`: **three wrapped functions, one
core-class extension and one module-attribute rebind.** MP1 (added by U3), MP2 (U7) and MP3 (H1) replace core
FUNCTIONS; the design doc budgeted zero, and those entries say why the stock
levers do not exist. MP4 (section 14) appends three fields to stock's `Product`
GRAPHENE TYPE, which is not a wrapped function and so was invisible to both the
count and the boot guard until 2026-09-11. MP5 (section 15) repoints one module
ATTRIBUTE, `saleor.graphql.views.schema`, so the query complexity guard weighs
the schema this fork actually serves. The compliance PLUGIN in section 9 is
a native hook, argued there, and is deliberately none of the four. Plus
one resolver swap Bill's secondary-categories patch performs from
`saleor/wsm/apps.py` (section 7). Core table edits: **zero.** Our tables carry
FKs into core tables; core migrations are untouched, and the one migration
section 7 brings in is state-only.

Sections 1 to 4, 6, 8 to 10 and 13 are this branch's own work and touch ONE
core file, `saleor/settings.py`. Sections 5 and 12 are withdrawn and their
lines are gone: section 13 turned the fork's urlconf into the root one, which
took `saleor/urls.py` back to upstream byte-for-byte, and the Django admin
deletion took the three `MIDDLEWARE` entries with it. Section 7 is the six WSM
patches that already existed before the fork, merged in from
`wsm/bakeoff-rebase-probe`; they are the reason the guard test's allow-list is
longer than one entry.

Since 2026-09-10 every patch also pins a **sha256 of the wrapped function's
own source** (`SOURCE` in `saleor/wsm/patches.py`), and `install_guard` refuses
to boot when a body has moved under a wrapper. `PINNED` says WHERE a function is
bound and cannot see that its contents changed, which is exactly the class that
bit this branch once already (the catalogue-promotion double-take fixed at
08b221ec). To re-pin after a deliberate upstream bump, read the upstream diff
for the function FIRST, then print the digests:

```
./run.sh shell -c "
import inspect, hashlib, importlib
from saleor.wsm import patches
for name in patches.PINNED:
    definer, _, attr = name.rpartition('.')
    fn = getattr(importlib.import_module(definer), attr)
    print(name, hashlib.sha256(inspect.getsource(fn).encode()).hexdigest())
"
```

`inspect.getsource` unwraps, so this answers about the ORIGINAL even after the
wrappers are installed.

Every patch this fork installs is named, once, in `saleor/wsm/patches.py` as
`PINNED`, by the defining module and qualname of the function it wraps, together
with every module that holds that function as an attribute. One entry per
function, not one per binding site.

Fork migrations, after the integration squash: **one 0001 per app, plus what
has landed since.**

| App | File |
|---|---|
| `wsm_compose` | `saleor/wsm/compose/migrations/0001_initial.py` |
| `wsm_dealer` | `saleor/wsm/dealer/migrations/0001_initial.py` |
| `wsm_containers` | `saleor/wsm/containers/migrations/0001_initial.py` |
| `wsm_containers` | `saleor/wsm/containers/migrations/0002_kitmemberrule.py` |

`0002_kitmemberrule` adds the cross-member rule a merchant writes on a kit (one
table plus its members M2M, both `wsm_containers_`-prefixed), and no core table
is touched by it.

Nothing else. The 0002s and 0003s the unit branches wrote are gone: they had
only ever run on bake-off databases, so the three apps were regenerated from
their models rather than carrying a history no deployment has. Each 0001 is the
whole app, constraints included, and the `wsm_dealer` one carries the
CheckConstraint `wsm_dealer_tier_amount_at_least_a_cent`.
`makemigrations --check` is clean and `migrate --check` is clean on
`saleor_bakeoff_merge`, which was fake-reset to zero and then fake-initialed
onto the squash. The two state-only migration packages described below,
`saleor/wsm/compose/django_auth_migrations/` and `saleor/wsm/migrations/` from
section 7, are separate and untouched by this.

Both counts are assertions, not claims.
`saleor/wsm/tests/test_core_tables_untouched.py` discovers the patches actually
installed in the running process by sweeping `sys.modules` for a wrapper whose
code lives under `saleor/wsm/`, and fails on any that `PINNED` does not name.
That sweep believes the `__code__` of a value only when it is a real
`types.CodeType`: a test double left in a core module namespace answers every
attribute it is asked for, so trusting the answer made the guard die on
`co_filename` under a full-suite run instead of reporting, and a guard that
errors is a guard that has stopped guarding. It
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

## 2. `saleor/settings.py`, +10 lines, 2 of them code (U2, 2026-09-08; cut back 2026-09-10)

**The Django admin this section existed for is gone.** The merchant UI is now
native screens in the Saleor Dashboard over `saleor/wsm/graphql`, ruling Dana
2026-09-10. Deleted with it: the 13 `ModelAdmin` registrations, the three
`admin.py` files, `saleor/wsm/admin_pickers.py`, `ComposeAdminSite` and its
mixins, the throttled admin login form, the `^admin/` mount in
`saleor/wsm/urls.py`, `saleor/wsm/middleware.py` (section 12, now empty) and
the admin's own form layer, `saleor/wsm/compose/forms.py`.

Four `INSTALLED_APPS` entries went with them, restoring this list to upstream's
for those four names:

| Removed | Why it was there |
|---|---|
| `django.contrib.admin` | the admin itself |
| `django.contrib.sessions`, `django.contrib.messages` | the two apps the admin requires |
| `django.forms` | Saleor sets `FORM_RENDERER = TemplatesSetting`, so admin widget templates resolved through the loaders |

Removing `django.forms` is what proved the form layer had no consumer left: the
one test that still built an inline formset raised
`TemplateDoesNotExist: django/forms/errors/list/default.html` on
`non_form_errors()` and nothing else moved. The rules it was asserting are
model rules (`configured_floor_cents`, `duplicate_fragment_error` in
`saleor/wsm/compose/models.py`) and the GraphQL option-set mutation calls the
same two, across the whole submitted list, with its own tests. So the module
went rather than the `INSTALLED_APPS` entry coming back. `currency_for`,
`money` and `CENT` lived there too and had exactly one caller each, all of them
in the deleted `admin.py` files; a later reader who wants a currency helper
should put it in `saleor/wsm/money.py`, which already owns the rounding rule.

`django.contrib.sites`, `django.contrib.staticfiles` and `django.contrib.postgres`
are NOT fork additions: upstream `a1ab3a23a3f5` installs all three and stock
Saleor imports them (`saleor/core/notification/utils.py`,
`saleor/checkout/complete_checkout.py`, `STATICFILES_FINDERS`). They stay.
`TEMPLATES` is byte-identical to upstream and always was; the old version of
this table claimed three added context processors that are not in the file.

**What is still here, and its consumer.**

| Lines | What | Read by |
|---|---|---|
| `INSTALLED_APPS`: `saleor.wsm.compose.apps.WsmAuthConfig` | `django.contrib.auth`, relabelled `django_auth` | the two state-only migrations below, and `Permission` rows the backend answers from. Stays BELOW `saleor.account` so auth's `createsuperuser` cannot shadow Saleor's (section 10) |
| `AUTHENTICATION_BACKENDS`: `saleor.wsm.compose.auth.AdminPasswordBackend` | password sign-in, and `wsm_*` permissions out of Saleor's renamed `permission_permission` | every `authenticate()` and every `has_perm()` in the process |
| `MIGRATION_MODULES = {"django_auth": "saleor.wsm.compose.django_auth_migrations"}` | `saleor.auth` already owns the `auth` label | `migrate`; without it, duplicate migration history |

Why a relabelled `django.contrib.auth` rather than the stock one: `saleor.auth`
is a models-free shim occupying the `auth` label whose `0013` deletes Group,
Permission and User from migration state. Two apps cannot share a label, so the
one we add takes `django_auth`. Its `AppConfig.ready()` is a no-op so that
Django's `create_permissions` receiver is never connected: `wsm_compose`
permission rows are created by our own `post_migrate` receiver
(`create_wsm_permissions`), into `saleor.permission.models.Permission`.

`MIGRATION_MODULES` points `django_auth` at a FORK-OWNED migration package,
`saleor.wsm.compose.django_auth_migrations`. It holds two migrations,
`0001_initial` and `0002_auth_models_state_only`, and both are state-only: they
create no table and touch no row, they exist so that `makemigrations --check`
sees a history for an app that has three models in the registry and none of its
own tables.

Read the `AUTHENTICATION_BACKENDS` row as the app-wide change it is. A backend in
that list is consulted on EVERY `authenticate()` call in the process, so this one
password backend is part of the shop's whole sign-in path. Since review wave A it
therefore honours the merchant's own `SiteSettings.password_login_mode`:
`DISABLED` refuses everyone and `CUSTOMERS_ONLY` refuses staff, which is what the
same switch means to Saleor's own backends (`saleor/wsm/compose/auth.py`, and its
tests in `saleor/wsm/compose/tests/test_auth.py`).

App-wide covers every `has_perm()` too, not only every `authenticate()`. Django's
`_user_has_perm` walks `AUTHENTICATION_BACKENDS` until one says yes, and this
backend is last, so every Saleor permission that SALEOR denied arrives here
next: an app token without `manage_channels`, a staff user asked for a field
they cannot see, every optional-permission check on the API. It used to load its
whole `wsm_` grant set to answer, one `permission_permission` join per request,
to discover that `product.manage_products` was never in a set that by
construction only holds `wsm_*`. Since the fork regression sweep (2026-09-09)
`has_perm` answers `False` off the permission string before any read. That one
query was the entire difference between this fork and upstream on
`test_retrieve_channel_listings` (17 queries against 16) and
`test_stocks_bulk_update_queries_count` (13 against 12), which is why those two
benchmark counts stay at upstream's numbers instead of being raised. The answers
did not change, only what they cost. Held by
`test_a_saleor_permission_is_refused_without_a_query` and
`test_a_wsm_permission_is_still_read_from_the_grant` in
`saleor/wsm/compose/tests/test_auth.py`, the first of which is red without the
short-circuit and the second red if it is widened.

**Open, and deliberately not settled here.** Nothing calls
`AdminPasswordBackend.authenticate()` now that the admin login view is gone, and
the only reader of the `wsm_*` grant rows was the admin's own permission checks.
Whether that whole stack (this backend, `create_wsm_permissions`,
`django.contrib.auth` and its two state-only migrations) survives depends on
decision D1 in `ARCHITECTURE-dashboard-merchant-screens.md`: per-model `wsm_*`
permissions on the GraphQL layer keep it, reusing `MANAGE_PRODUCTS` and
`MANAGE_DISCOUNTS` deletes it. It was left in place because deleting it means
deleting migrations, which this branch does not do on a live schema.

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
so its tables carry the `wsm_containers_` prefix and stay out of core's
namespace. Everything else in Containers (models, migration, pricing, views,
admin, tests) lives under `saleor/wsm/containers/`, which upstream does not own.

This is the SHARED touch site: U1, U3 and U4 each add their own line in the
same block. The merge kept all three, in unit order.

Removal cost: delete the two lines and the package. Nothing in core references
Containers.

---

## 5. Withdrawn. `saleor/urls.py` is upstream again (U1 spine, 2026-09-10)

This section used to hold the fork's one `saleor/urls.py` line,
`re_path(r"", include("saleor.wsm.urls"))`. Section 13 inverted that
relationship: `saleor/wsm/urls.py` is the ROOT urlconf now and `saleor.urls` is
what it includes, so the core line would be a cycle. It was removed and
`saleor/urls.py` is byte-identical to the upstream release again.

The numbering is kept rather than closed up, because sections 6 and 7 were
written referring to "sections 1 to 5" and a renumbered ledger is a ledger
nobody can follow across a rebase.

---

## 6. Nothing. What the merge added, and where (U6, 2026-09-08)

Landing all three units on one branch added **zero** lines to a file Saleor
owns. Sections 1 to 5 were the complete core-touch list at the time, and the
command at the top of this file then returned `saleor/settings.py`, the now
withdrawn `saleor/urls.py` and
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
  series facts (`brand`, `axes`, `partitioning_axis`, `miss_message`,
  `published`) are written
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
- **No `django.contrib.admin` registration in settings.** (Historical: all
  three `admin.py` files were deleted on 2026-09-10, section 2.)
  `saleor/wsm/containers/admin.py`
  registered onto the AdminSite that U2 mounted; on a containers-only branch there
  is nothing to register onto and the screens are exercised by calling
  `admin.register(AdminSite())` in a test. When U2 and U4 sit on one branch the
  screens appear with no further edit. The `post_migrate` permission receiver in
  `saleor/wsm/compose/apps.py` needed `wsm_containers` in its label guard so a
  non-superuser merchant is grantable on them: done in section 6 below, and it
  was the whole merge cost, as predicted.

---

## 7. Bill's six pre-existing WSM patches, merged in (TNO pass 1, 2026-09-08)

Merged from `wsm/bakeoff-rebase-probe` (`e9ecb87`), which is the six patches
already carried on `webshopmanager/saleor` rebased onto 3.23.31. Unlike sections
1 to 6 these edit core `.py` files directly, which is precisely what "own our
Saleor" has to price. **662 lines of core code across 13 files**, plus test files
and the generated `schema.graphql`.

That 662 is THIS SECTION's six patches, not the branch. Measured at 23d393e3 the
whole non-test core footprint outside `saleor/wsm` is **749 added / 28 removed
across 15 files** (`git diff --numstat a1ab3a23a3f5 HEAD -- saleor/ | grep -v
saleor/wsm | grep -v /tests/`); the gap is sections 11 and 12, which are ours and
landed later (Wild West finding 12).

| Patch | Core files it edits | Lines |
|---|---|---|
| Legacy WSM5 password hashers, upgraded on first sign-in (WSM6-1978) | `saleor/core/hashers.py` | +121 |
| NULL-price channel listings must not crash variant pricing (WSM6-1178) | `saleor/graphql/product/types/products.py` | +5 |
| Braintree gateway crash on order details with missing credentials | `saleor/payment/gateways/braintree/plugin.py` | +22 |
| Secondary categories unioned into `Category.products` | none: lives in `saleor/wsm/` | 0 |
| An undeliverable destination is not "out of stock" | `saleor/warehouse/availability.py`, `saleor/checkout/error_codes.py`, `saleor/core/exceptions.py`, `saleor/graphql/checkout/mutations/checkout_create_from_order.py`, `saleor/graphql/checkout/mutations/utils.py`, `saleor/graphql/schema.graphql` | +161 |
| Validate a checkout is payment-ready before charging it | `saleor/checkout/checkout_cleaner.py`, `saleor/checkout/complete_checkout.py`, `saleor/graphql/payment/mutations/transaction/transaction_initialize.py`, `saleor/graphql/payment/mutations/transaction/transaction_process.py` | +373 |

Also in `saleor/wsm/`, so outside the core budget: `models.py`,
`secondary_categories.py`, `category_products.py`, `apps.py` (label `wsm`), and
**one migration, `saleor/wsm/migrations/0001_initial.py`**.

That migration is **state-only**. Its single `CreateModel` carries
`managed = False`, so Django emits no DDL for it; it declares
`product_secondary_categories`, a table the PartsLogic service creates and
populates with its own Go migrations and that Saleor only ever reads. No core
table is altered and no table is created by this branch. The guard test
`saleor/wsm/tests/test_core_tables_untouched.py` reads the `managed` option
rather than the table name for exactly this case, so a *managed* fork model
still has to be `wsm_`-prefixed.

Open question for the "own our Saleor" epic: `product_secondary_categories` is
the only fork-adjacent table without the `wsm_` prefix. Renaming it is one line
here and a coordinated change in PartsLogic's Go migrations and the 5.0
migration ETL, so it is a decision, not a cleanup. Left as Bill wrote it.

The secondary-categories patch also swaps `Category.resolve_products` at
`ready()`. It is not counted as a core *touch* because `saleor/graphql/` stays
byte-identical, but it is a monkey patch in the MP sense, and its copy of the
upstream resolver body is pinned by a source digest that fails on an upstream
rebase (`saleor/wsm/tests/test_patch.py`).

---

## 8. `saleor/settings.py`, +5 lines, 1 of them code (review wave A, 2026-09-08)

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

That read used to take a `?customerId=` of its own and quote that customer's
dealer deltas, which left the same hole open on the one route with no key on it:
a `User` global id is base64 of a sequential integer, so anyone who could reach a
product page could walk the customer table and read the dealer price book one
buyer at a time. Since review wave WW1 it answers RETAIL to everyone and the
parameter is gone, along with the tier prefetch and the per-value tier lookup it
fed. A dealer sees their own deltas from the key-gated add, which is where the
money is taken and where the caller has already been authenticated. Guard test:
`test_option_sets_never_quotes_a_dealer_delta_to_the_public`.

Unset or empty fails SAFE: every gated endpoint answers 401. A tenant that forgot
to set the secret sells nothing through these routes, which is loud, rather than
selling at anyone's dealer price, which is silent.

Removal cost: delete the line, the decorator and `saleor/wsm/http.py`.

---

## 9. `saleor/settings.py`, +5 lines, 1 of them code (U8, 2026-09-09)

One entry appended to `BUILTIN_PLUGINS`:
`saleor.wsm.compose.plugin.ComposeCompliancePlugin`. The other four lines are
the comment that says why.

**What it buys.** Shipping restrictions have to be enforced where a checkout
becomes an order, and `preprocess_order_creation` is the only plugin-manager
hook that fires there. Core calls it on all three completion paths
(`complete_checkout.py:754` and `:987`, `checkout_cleaner.py:467`, which is the
`orderCreateFromCheckout` path the storefront actually uses), and both call
sites catch `TaxError` only, so the `ValidationError` our plugin raises reaches
the mutation and comes back as a checkout error.

**Why this is not a fourth monkey patch.** MP3's own notes rejected this hook
for REPRICING, because a plugin here can refuse an order but cannot correct
one. A restriction only ever wants to refuse. So the seam MP3 could not use is
exactly the right one for this, and the fork gains a behaviour with a settings
line instead of another wrapped core function.

**Cost.** One more plugin class in the manager's list. The method itself runs
once per order creation and never on a browse, PDP, cart or shipping-step
request; inside it, one indexed read of `wsm_compose_productcompliance`, and a
second for the zone allow-list only when a product in the order carries a row.

**Verified by** `test_the_registered_plugin_refuses_a_california_order` in
`saleor/wsm/compose/tests/test_compliance.py`, which goes through
`get_plugins_manager()` rather than the plugin class, so the registration is
part of what is proved.

## 10. `saleor/settings.py`, one line moved, +7 comment lines (2026-09-09)

`saleor.wsm.compose.apps.WsmAuthConfig` moved out of the admin block at the top
of `INSTALLED_APPS` to sit directly below `saleor.account`. No behaviour line
was added or removed: the app is still installed, still under the label
`django_auth`, still with `MIGRATION_MODULES` pointing at the fork module.

**The regression it fixes.** `saleor/account/tests/test_account_command.py`
passed 3/3 on upstream `a1ab3a23a3f5` and failed 3/3 on `wsm/bakeoff` with
`AttributeError: 'UserManager' object has no attribute 'create_superuser'`.
Django resolves a management command name to the first app in `INSTALLED_APPS`
that ships one (`get_commands()` walks `reversed(app_configs)` and overwrites,
so the earlier app wins). `django.contrib.auth` ships `createsuperuser` and
`changepassword`, and `saleor/account/management/commands/` overrides both.
Installed above `saleor.account`, auth won, and Django's own `createsuperuser`
calls `_default_manager.create_superuser()`, which Saleor's `UserManager`
(`saleor/account/models.py:132`, a `BaseUserManager`) does not define. Below
`saleor.account`, Saleor's command wins again, which is upstream behaviour.

**Why the fix is here and not in the wsm layer.** The broken value is a list
order, and `INSTALLED_APPS` is the layer that owns it. The alternatives all add
code to work around that order: a `create_superuser` shim on Saleor's manager
(a core edit, for a command the fork does not even use), overriding the auth
AppConfig's `path` so its `management/` directory is not found (which also
hides auth's templates and locales), or monkey patching `get_commands`. Moving
the entry fixes the whole class: any command `django.contrib.auth` ships now
defers to a `saleor.account` override.

**Verified by** the three upstream tests above, red then green on a fresh test
DB, plus `saleor/wsm/tests/test_management_commands.py` (new, fork-owned, two
asserts), which pins `createsuperuser` and `changepassword` to `saleor.account`
and was proven able to fail: it is red on `wsm/bakeoff` before the move.

## 11. `saleor/graphql/payment/mutations/transaction/transaction_initialize.py`, +16 lines, 5 of them code (fork regression sweep, 2026-09-09)

`TransactionInitialize.validate_checkout` now takes the payment gateway's
identifier, and returns `None` without running the payment-readiness gate when
that identifier is `GIFT_CARD_PAYMENT_GATEWAY_ID`. The gate itself, and every
other caller of it, is unchanged.

**The regression it fixes.** Three tests passed on upstream `a1ab3a23a3f5` and
failed on `wsm/bakeoff`, all three with
`Checkout is not ready for payment: it has no available lines.`:
`test_for_checkout_with_gift_card_payment_gateway_rejection_keeps_owner_authorization`
in `saleor/graphql/payment/tests/mutations/test_transaction_initialize.py`, and
`test_gift_card_detach_gift_card_from_checkout_as_gift_card_transactions_are_about_to_get_charged`
and
`test_gift_card_detach_gift_card_from_checkout_as_checkout_gets_completed` in
`saleor/tests/e2e/gift_cards/test_gift_cards.py`. The gate section 7 installed
calls `fetch_checkout_lines` and refuses a checkout that has none. Saleor
attaches a gift card to a checkout THROUGH `transactionInitialize`, and both its
own e2e tests and that unit test do so against a checkout carrying no lines at
all, because attaching a card is not a step that requires a payable cart.

**Why the fix is here and not in the gate.** The gate exists to stop an external
payment APP charging real money against a checkout that can never become an
order, leaving a charge with no order and no automatic recovery. The gift card
gateway is not one. `clean_app_from_payment_gateway`, ten lines up in the same
class, returns `None` for this identifier precisely because there is no app:
Saleor is the gateway, the balance it moves is Saleor's own ledger, and Saleor
releases and detaches that authorization itself when the checkout does not
complete, which is exactly the behaviour the two e2e tests above exist to prove.
Nothing outside is charged, so nothing can be stranded, and the snapshot the
gate returns has no outside charge for a later total to be reconciled against.
Narrowing the gate by gateway rather than relaxing its `not lines` clause leaves
every payment-app path byte-for-byte as the patch shipped it.

**Not `TransactionProcess`.** Its copy of the gate is untouched, because a gift
card transaction can never get THROUGH it: `clean_payment_app` requires an `App`
row carrying the transaction's identifier, and no such row exists for the
built-in gateway, so that mutation refuses a gift card whatever the readiness
gate says. Upstream, with no gate at all, refuses it in the same place.

Corrected 2026-09-10 (Wild West finding 10, checked and NOT reproduced): this
paragraph used to say the refusal happens BEFORE `validate_checkout`, and the
call order does not support that. `perform_mutation` calls `validate_checkout`
at line 196 and `clean_payment_app` at line 218, so on a checkout that is not
payment-ready the readiness error is the one returned. Both are refusals of a
call that cannot succeed either way, so the asymmetry is not a defect and the
gate was left alone rather than growing a second core edit for it. The
reachability argument is now held by a test rather than by this paragraph:
`saleor/wsm/tests/test_payment_readiness.py`. If an `App` row ever comes to
carry the built-in gift card identifier, that test goes red and the carve-out
has to be carried across.

**Verified by** the three tests above, red then green on a fresh test DB, run
together with the whole of `test_transaction_initialize.py`,
`test_transaction_process.py`, `saleor/checkout/tests/test_checkout_cleaner.py`,
`saleor/graphql/checkout/tests/mutations/test_checkout_complete_with_transactions.py`
and `saleor/tests/e2e/gift_cards/`: 271 passed, so the gate's own proofs still
hold.

## 12. `saleor/settings.py`, `MIDDLEWARE` back to upstream's three (fork regression sweep 2026-09-09; REMOVED 2026-09-10)

**Nothing left. This section is now a record of a touch that no longer exists.**

The fork put `SessionMiddleware`, `AuthenticationMiddleware` and
`MessageMiddleware` in `MIDDLEWARE` because the Django admin hard-requires them.
Listed unscoped they ran on EVERY request the process serves, and
`AuthenticationMiddleware` overwrote `request.user` with a lazy `AnonymousUser`,
which broke upstream's plugin-requestor contract on the live GraphQL API
(`saleor/core/tests/test_dataloaders.py`, 2/2 red on the fork, 2/2 green on
upstream `a1ab3a23a3f5`). The 09-09 fix repointed the three at
`/admin/`-scoped subclasses in `saleor/wsm/middleware.py`.

The admin is gone (section 2, ruling Dana 2026-09-10), so the three entries and
`saleor/wsm/middleware.py` are gone with it. `MIDDLEWARE` is byte-identical to
upstream's again:

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "saleor.core.middleware.jwt_refresh_token_middleware",
]
```

This is the prize the deletion bought: the section existed only because the
admin needed three middlewares the API did not, and deleting the admin deleted
the reason rather than the symptom.

**Verified by** the same two upstream tests, which pass on a `MIDDLEWARE` with
no fork entry in it at all, and by `manage.py check`, which no longer has an
`admin.E40x` family to trip.

# Monkey patches

Expected: zero. Actual: **five**: three wrapped functions (MP1 in U3, MP2 in
U7, MP3 in the reprice wave), one core-class extension (MP4, the product tab,
section 14) and one module-attribute rebind (MP5, the query cost guard, section
15). Every entry names the exact function, class or attribute it changes and the
upstream change that would delete it.

## 13. `saleor/settings.py`, one line repointed, +5 comment lines (U1 dashboard spine, 2026-09-10)

```python
ROOT_URLCONF = "saleor.wsm.urls"
```

**This is the whole attachment for the fork's GraphQL layer.** The merchant
screens are being rebuilt as native Saleor Dashboard sections, which means the
Dashboard has to reach our models over GraphQL, which means our fields have to
be in the schema the API serves. There are two ways to do that in a fork that
does not edit `saleor/`: rebind `saleor.graphql.api.schema` from `ready()`
(rung 5, a fourth monkey patch), or compose our own schema and make our own
urlconf the root (rung 1, a settings seam). This is the second.

What it buys, concretely:

- `saleor/wsm/graphql/schema.py` SUBCLASSES stock's `Query` and `Mutation`
  (`type("Query", (WsmQueries, api.Query), {})`) and calls
  `build_federated_schema` with the arguments read back off `api` itself, so
  the composed schema is stock's schema plus our fields, never stock's schema
  changed. `saleor/graphql/api.py` is imported and left alone; its own
  `schema` object still exists and still has no WSM field on it, which
  `test_our_fields_are_on_our_schema_and_stock_is_left_alone` asserts.
- `saleor/wsm/urls.py` serves that schema at `graphql/` FIRST, then includes
  `saleor.urls` whole. Every core route resolves exactly as before; core's own
  `graphql/` line is simply never reached.
- The monkey-patch count stays at **three**. A fourth was the alternative.
- `saleor/urls.py` LEAVES the deviation budget (section 5, withdrawn), so the
  files outside `saleor/wsm/` that differ from upstream on this branch's own
  account go from two to one, and `ALLOWED_CORE_FILES` in
  `saleor/wsm/tests/test_core_tables_untouched.py` is one entry shorter.

**The tripwire.** Composing "with the same arguments" is an assumption about a
core file. `API_SCHEMA_SOURCE` in `saleor/wsm/graphql/schema.py` is a sha256 of
api.py's construction block (from `schema = build_federated_schema(` through
`monitor_fields_usage(schema)`), and
`test_the_pinned_digest_is_the_api_block_this_layer_was_written_against`
compares it on every run. An upstream bump that adds a `types=` entry or a
directive is then a red test with a diff to read, rather than a type that
exists in stock's schema and silently not in the one we serve. Proven able to
fail twice over: once by pinning a wrong digest (the first run of this branch),
and permanently by
`test_the_tripwire_moves_when_an_argument_does`, which feeds the function a
modified api.py and asserts the digest moves. To re-pin after a deliberate
bump, read the diff FIRST, mirror any new argument, then:

```
python -c "from saleor.wsm.graphql import schema; print(schema.source_digest())"
```

**Cost, measured and accepted.** Two full Saleor schemas are built at urlconf
load, because `saleor.urls` builds its own on import and this design refuses to
patch that away. The ceiling is cold-start time and resident memory on a worker,
nothing per request. The upgrade path, if a cold start ever has to get cheaper,
is the rung-5 rebind with a `PINNED` entry, which buys back one build at the
cost of one monkey patch.

**Removal cost:** set `ROOT_URLCONF` back to `"saleor.urls"`, restore the
`include("saleor.wsm.urls")` line in `saleor/urls.py`, and the fork's REST
routes work exactly as they did before; only the GraphQL fields go away.

---

## 14. MP4. Three fields appended to stock's `Product` graphene type (dashboard spine, 2026-09-10; ledgered 2026-09-11)

`saleor/wsm/graphql/compose/product_extension.py` writes into
`Product._meta.fields` and sets `resolve_<name>` on the class, from our own
module, at import. No file under `saleor/graphql/` is edited.

**Why it exists.** The Compose tab on the product page wants the product's
option sets, its charges and its disclosure in the SAME round trip the page
already makes. A graphene 2 `ObjectType` collects its fields when the class body
runs, and `_meta.fields` is the only public handle afterwards.

**Why it was not in this ledger until now.** It wraps no function, so
`patches.installed()` cannot see it, and the count above read "three, nothing
here adds one". That sentence was in the PR body too. The Marshal review of fork
PR 4 (2026-09-11) called it: a claim the code contradicts is the most expensive
sentence in a description.

**The tripwire, now the same as the other three.** `EXTENDED` in
`saleor/wsm/patches.py` pins the type and the field names;
`patches.extensions_installed()` DISCOVERS the real set by sweeping loaded core
modules for a field carrying `_wsm_owned`; and
`test_the_installed_core_class_extensions_are_the_documented_ones` compares
them. An extension that lands without a line here reddens that test in the run
that adds it.

**Removal cost.** One module deleted and the three fields declared wherever
Saleor grows a real extension hook. Nothing else in the package changes;
`_assert_free` already refuses to overwrite a stock field, so an upstream bump
that adds a `wsmFees` of its own is a loud `RuntimeError` at import rather than
a silently shadowed resolver.

**Known boundary: there are two schemas, and webhooks use the other one.**
`saleor/graphql/webhook/subscription_payload.py:123` and `:209`, and
`saleor/webhook/observability/obfuscation.py:200`, all do `from ..api import
schema` and parse the subscription document against STOCK's schema object, not
the composed one `saleor/wsm/graphql/schema.py` builds and `ROOT_URLCONF`
serves. So an app subscription cannot select `wsmFees`, `wsmOptionSets` or
`wsmCompliance` on `Product`, and `anonymize_event_payload` parses against a
schema that cannot see them. The Dashboard is unaffected (it goes through the
served schema); apps subscribing to WSM fields are not supported today. The
follow-up, if an app ever needs it, is to pass the composed schema into those
two call sites behind the same digest pin `API_SCHEMA_SOURCE` already uses.

## 15. MP5. `saleor.graphql.views.schema` repointed at the composed schema (fix wave 2A, 2026-09-11)

`saleor/wsm/graphql/cost.py` `install()` sets `saleor.graphql.views.schema` to
the schema `saleor/wsm/graphql/schema.py` builds, from that module, the moment it
is built. One attribute, no function wrapped, no core file edited.

**Why it exists.** `saleor/graphql/views.py:38` does
`from .api import API_PATH, schema`. Line 358 PARSES the incoming document with
`self.schema`, the schema the view was constructed with, and line 479 costs it
with `validate_query(schema=schema, ...)`, the module global. On stock Saleor
both names point at one object and the difference is invisible. On this fork the
view mounted at `graphql/` serves the COMPOSED schema, so the cost walk ran over
a schema in which no `Wsm*` field exists: `products(first: 100)` cost 200 and
`wsmDealerGroups(first: 100)` cost 0, measured 2026-09-11. The whole WSM surface
sat outside `GRAPHQL_QUERY_MAX_COMPLEXITY`. A cost map alone cannot fix it, and
in fact cannot be installed without it: `validate_cost_map` walks the same
schema and rejects the entries ("cost map contains a field wsmOptionSet not
defined by the Query type"), which turns every request into an error. That was
the red.

**Blast radius.** The composed schema is stock's `Query` and `Mutation`
SUBCLASSED, so it is a superset: every stock query is weighed against exactly
the types it was before, with the same map entries. Nothing else in `views.py`
reads that module global.

**The tripwire.** `REBOUND` in `saleor/wsm/patches.py` pins the name;
`patches.rebindings_installed()` DISCOVERS every core module attribute holding
an object the fork marked `_wsm_owned`; and
`test_the_core_attributes_this_fork_rebinds_are_the_documented_ones` compares
them. A second rebind is a red test in the run that adds it.

**Removal cost.** One line, the moment upstream costs the query with
`self.schema`. The upstream change that deletes this is a one-word diff in
`saleor/graphql/views.py:479`, and it is worth offering: on stock Saleor the two
names are the same object, so passing the view's schema is a no-op there and a
fix for every fork that composes one.


## 16. Nothing. Container slots and the vehicle resolver (U7, 2026-09-11)

The slots layer added **zero** lines to a file Saleor owns. The command at the
top of this file still returns `saleor/settings.py` and `saleor/wsm/**` and
nothing else, on this branch, after the rebase onto `ca1fbf3771`. New monkey
patches: **zero.** New core rebinds: **zero**, so `REBOUND` in
`saleor/wsm/patches.py` is unchanged and MP5 is the only entry in it.

Everything the unit added lives under `saleor/wsm/`: a `ContainerSlot` table
and its members' foreign key (`wsm_containers.0003_container_slots`, additive,
with a data step that gives every existing kit one slot holding its members),
the resolver in `saleor/wsm/containers/resolve.py`, and the GraphQL that
reaches them.

The one thing it had to add OUTSIDE its own package is three cost-map entries,
because fix wave 2A's `test_every_wsm_root_field_carries_a_cost_hint` reads the
SERVED schema and reddens on a `wsm*` root field with no price. They are in
`saleor/wsm/graphql/cost.py`, which is fork-owned:

| Entry | Weight | Why that weight |
|---|---|---|
| `Query.wsmContainerResolve` | `PER_OBJECT` | One container resolved for one vehicle. The answer is one object however many members it holds, and the engine round trips are capped at two, so it weighs what `wsmKitConfig` weighs. |
| `WsmKitConfig.slots` | `PER_OBJECT` | An unpaged list on a type reached through a PAGED connection, so it multiplies with the page above it. Same shape `COMPOSE_COST` prices on stock's `Product`. |
| `WsmContainerSlot.candidates` | `PER_OBJECT` | Same, one level down: this is where a kit's fan-out actually is. |

Known gap, pre-existing and NOT introduced here: `WsmKitConfig.members` and
`WsmKitConfig.rules` have no entry either, so they are still free. The 2A test
only asserts root fields, so nothing is red. Worth one line in `cost.py` the
next time that file is opened.


## Where the stamps live: PRIVATE metadata, always (review wave WW1, 2026-09-08)

All three patches decide what to do with a line by reading a `wsm.*` key off it.
Every one of those keys lives in `private_metadata`, written only by the five
key-gated endpoints and by MP3 itself, and none of the three ever reads the
public copy.

**The exploit, before this.** `saleor/graphql/meta/permissions.py` maps
`CheckoutLine` PUBLIC metadata to `no_permissions`: any caller can
`updateMetadata` a line in a checkout they hold the token for, which is their
own cart, with no key, no login and no account. The stamps were public, so a
shopper could write `wsm.dealer` = `{"group": "..."}` onto their own line and
MP3's anonymous branch would price it against that group's ladder. Measured
live: a 10.00 line came back at 1.00. Group codes are short, guessable, and
leak through the storefront anyway. The same key inverted the other way: a real
dealer could DELETE it and stack a voucher on top of a tier price, because MP1
and MP2 find a dealer line by the presence of that key.

**After.** The authority copy is private, and `PRIVATE_META_PERMISSION_MAP`
gates `CheckoutLine` behind `MANAGE_CHECKOUTS`. The forgery still SUCCEEDS as a
mutation (refusing it would mean editing a core permission map, and B8 says
zero core table edits and a minimum core footprint), it just buys nothing: MP3
prices from the private copy, and reprice deletes the forged public key on the
way past so it cannot mislead a support screen or ride into the order.
`create_order_from_checkout` copies private metadata onto the order line, so
MP2 still finds the stamp after completion.

**The public copies that remain are display.** The storefront cart and order
screens pair fee lines to parents and render chosen options off public line
metadata (`wsm-storefront src/lib/composeFee.ts`, `src/lib/order-grouping.ts`),
and acceptance bar B6 is "no storefront code". So `compose.fee`,
`compose.parent_line`, `wsm.options`, `wsm.options.sku`, `wsm.options.cid`,
`wsm.options.acc`, `bundle_kit_group_id` and `wsm.kit` are written to BOTH, and
the money path reads only the private one. Forging a public copy changes what
your own cart draws and no number anywhere. `wsm.dealer` has no storefront
reader and therefore has no public copy at all.

**Verified by** `test_a_dealer_stamp_a_shopper_wrote_for_themselves_buys_nothing`
in `saleor/wsm/tests/test_reprice.py`, which drives the forgery through the
stock `updateMetadata` mutation unauthenticated, asserts the mutation is
accepted, and then asserts the price does not move.

## MP1. Undiscountable lines are left out of line discounts, cart and order

Installed by `saleor/wsm/dealer/apps.py` `DealerConfig.ready()`; the whole patch
is `saleor/wsm/dealer/no_stacking.py`. Three functions are wrapped, none is
reimplemented: each wrapper calls the original with a smaller list of lines.

**Which lines get left out is one function, `split_discountable`**, and MP1 and
MP2 both read it, so the line-level half and the order-level half cannot drift
apart. It excludes two kinds of line:

- Any line carrying the private `compose.fee` stamp, always. A fee is money the
  merchant passes through (crating, core, environmental) rather than margin, and
  in 5.0 a coupon came off the merchandise subtotal and never off a product fee.
  There is no toggle on this one, because "discount my crate charge" has no
  merchant reading that a discount on the merchandise line cannot express.
  Required and optional charges are treated alike: `required` lives on the `Fee`
  row and is deliberately not snapshotted onto the line (the stamp carries the
  label and `apply_to` only), and the answer would be the same if it were.
- Any line carrying the private `wsm.dealer` stamp, while the merchant leaves
  discount stacking off. That is requirement 2.4 and its toggle, unchanged.
- On the CATALOGUE paths only, any line the fork priced FROM the listing's sale
  price: `price_override_reason` of `wsm.containers` (a kit member) or
  `wsm.compose` (a configured line or its charge). The promotion is already
  inside the number written to `price_override`, so stock Saleor taking the
  same rule off that override charges the merchant's one sale twice. Measured
  live on the Tonneau Outlaw stage 2026-09-09: an Alba RZR900 kit member
  overridden at 1899.99 under a 254.01 rule billed at 1645.98, and the kit at a
  subtotal of 2278.98 instead of 2532.99. A VOUCHER still stacks on those
  lines, which is why this is an argument to `split_discountable` and not a
  third clause inside it: a voucher is a discount the merchant does mean to
  give on top of a sale price, and only the caller knows which rule it is
  under.

The toggle is read at most once per call and only once a dealer line has
actually been seen, so a cart of retail lines and charges still costs no
settings query, and a cart with neither kind takes the original path on the
original arguments.

| Replaced function | Module | What the wrapper does |
|---|---|---|
| `attach_voucher_to_line_info(voucher_info, lines_info)` | `saleor/discount/utils/voucher.py` | Runs the original, then clears `voucher` and `voucher_code` from every line info `split_discountable` excludes. |
| `prepare_checkout_line_discount_objects_for_catalogue_promotions(lines_info)` | `saleor/discount/utils/checkout.py` | Calls the original with the discountable lines only, and adds any catalogue discount already sitting on an excluded line to the returned removal list. |
| `prepare_order_line_discount_objects_for_catalogue_promotions(lines_info)` | `saleor/discount/utils/order.py` | The same, on the order path's own copy of that code. Both wrappers call one `_catalogue_split`, so which lines are left out is decided once; they differ only in the width of the answer the original returns, the order path having no promotion end date to carry. |

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
once a dealer line is present, and then it is read from its own row: ONE indexed
single-row query per guard that fires, never on a retail cart. It used to be
cached in a process global with a `post_save` signal to clear it, which is only
true for the one process the save happened in. Gunicorn runs several workers and
celery prices too, so a merchant turning stacking off in the console left every
other worker stacking a discount onto dealer prices until it cycled, and which
price a shopper got depended on which worker took the request. A `.update()`,
which a bulk action or a data migration uses, reached nobody at all. One query is
cheaper than a wrong price.

Upstream change that deletes this file: a documented per-line discount exclusion
on the checkout path, for example a `CheckoutLine.discounts_excluded` flag or a
`can_discount_line(line_info)` predicate honoured by both
`attach_voucher_to_line_info` and both
`prepare_*_line_discount_objects_for_catalogue_promotions`, the way
`is_gift` already is.

**Why the order path needs its own wrap.** `saleor/discount/utils/order.py` is a
second copy of the checkout catalogue code, run on every draft order
recalculation and every order edit, and it was left unguarded when the
sale-price rule landed. `create_order_from_checkout` copies
`price_override_reason` and the private stamps onto the order line, so all three
predicates already answer on an order line and the wrap costs one more entry in
`PINNED`. Without it a kit member the cart charged 1899.99 fell back to 1645.98
the moment the checkout became an order, and a dealer line lost its tier price
to a promotion the cart had kept off it: the shopper is shown one price and
billed another, and the order is the one they are billed.

Scope: this covers LINE-level discounts (catalogue promotions, `SPECIFIC_PRODUCT`
and `apply_once_per_order` vouchers), on the checkout and on the order.
ENTIRE_ORDER vouchers and order promotions
are checkout-level discounts in Saleor, they never reach a line, and MP1 leaves
them alone. U3 filed that gap rather than fixing it; MP2 below closes it.

---

## MP2. Dealer lines and fee lines are excluded from ORDER-LEVEL discounts

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

Rebinding: `import` binds a name, so swapping the defining module's attribute
leaves every module that imported the function by name still calling the
original. `propagate_order_discount_on_order_prices` is imported into
`saleor.plugins.manager`, and `create_discount_objects_for_order_promotions` into
`saleor.discount.utils.checkout` and `saleor.discount.utils.order`; the other
three are held only by the module that defines them.

All five are rebound at every site they are found at, by the same sweep MP1 uses.
The sites are pinned in `saleor/wsm/patches.py` next to the function they belong
to, and DISCOVERED from `sys.modules` at `ready()`: a discovered set that differs
from the pinned one raises `ImproperlyConfigured` before the process serves a
request. So an upstream bump that adds a sixth import site is a boot error naming
the function and the new module, rather than an order-level discount that quietly
lands on a dealer line. The sweep matches by identity, not by name, so
`import ... as` cannot hide a site either.

Which lines are handed to the original is MP1's `split_discountable` in every one
of the five, so a fee line is out of the AMOUNT and out of the SPREAD on the
checkout and on the order alike.

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

**What the live re-walk found with only the dealer half in place (probe P10,
2026-09-08).** A cart holding one dealer-priced configured item and the required
crate charge that comes with it, `CRATE-01` at 149.00, and a 100.00 fixed
ENTIRE_ORDER voucher. MP2 kept the discount off the dealer line, correctly, and
Saleor then put the whole 100.00 on the only other line in the cart: the charge
billed at 49.00. A voucher of 149.00 or more would have zeroed a mandatory
pass-through charge outright, and a merchant issuing a 200.00 code would have
been paying the crate company out of margin without a number on any screen
saying so. Retail carts were open the same way and more widely, because a retail
line is not excluded from anything: every fee line in the fleet was discountable,
so a percentage code took its cut of every crating and core charge in every cart
that carried one.

Now the charge is excluded on both halves, and the money lands where 5.0 put it.
On the P10 cart nothing in it is discountable at all, so Saleor is handed an
empty line set, sizes the discount on a subtotal of zero and ACCEPTS the code at
0.00 rather than refusing it (`get_voucher_discount_for_checkout` raises
`NotApplicable` only from the voucher's own validation, and a code with no
minimum spend passes that on an empty set). The money is right and the shopper is
under-informed: they see the code accepted and the total unchanged. That is
acceptable for the bake-off and is the one deliberate rough edge in this rule; a
storefront message ("this code applies to merchandise only") is the fix and it is
storefront work, which acceptance bar B6 keeps out of this repo.

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
same one-row read MP1 uses, so a retail-only fleet pays nothing.

Upstream change that deletes this file: an exclusion honoured by the order-level
discount base, for example a `discountable` predicate on the line consulted by
`base_checkout_subtotal` and by both propagate functions, the way `is_gift` is
already consulted on the voucher side. That one predicate would delete MP1 as well.

---

## MP3. Every price this fork wrote is re-derived before it becomes an order

**Wrapped:** `saleor.checkout.calculations._fetch_checkout_prices_if_expired`
**Binding sites:** one, `saleor.checkout.calculations` (the function is private
to its own module; nothing else imports it by value).
**Installed by:** `saleor/wsm/reprice.py::install`, from `WsmConfig.ready`.
**Added by:** hardening H1, 2026-09-08.

### The hole

Compose and Dealer Pricing both write money onto a `CheckoutLine` as
`price_override`, and Compose additionally writes a separate fee line whose
`quantity` mirrors its parent's. Both numbers are a function of the line
QUANTITY, and both were computed once, at add time, by our own HTTP endpoint.
Core's own line mutations (`checkoutLinesUpdate`, `checkoutLinesDelete`) change
that quantity without ever calling us back. So:

- a dealer adds 10 at the 10-break price, drops the line to 1 with the stock
  mutation, and completes at the 10-break price;
- a shopper configures 2 of a kit with a per-unit fee, drops the parent to 1,
  and the fee line stays at quantity 2.

Neither is exotic: `checkoutLinesUpdate` is what every quantity stepper in a
cart calls. The storefront was expected to call our reprice endpoint after every
such change, which makes the correctness of a price a property of the CLIENT.
That is not a seam, it is a convention, and a convention is not a control.

### Why this seam and not another

Ranked by the standing preference, reuse a pinned patch point, then a
plugin-manager hook, then a new pinned patch:

- **MP1 and MP2** are discount exclusions on dealer lines. Neither is reached on
  a Compose-only checkout and neither sees quantity changes on a checkout with
  no discount. Not reusable.
- **`manager.preprocess_order_creation`** is the only plugin-manager hook that
  fires at completion. On the payment path it is called from
  `_prepare_order_data` AFTER the totals are computed, so a plugin there can
  REFUSE the order but cannot correct it. Refusing is the wrong default: the
  buyer did nothing wrong by using a quantity stepper.
- **The price plugin hooks** (`calculate_checkout_line_unit_price` and friends)
  only run under the `TAX_APP` strategy. On a flat-rate tenant they never fire.
- **`add_variants_to_checkout`** sees quantity changes but not sign-ins, not
  catalog edits, and not completion of a checkout nobody touched today.

`_fetch_checkout_prices_if_expired` is the single funnel every price
recalculation in core goes through, on the cart read, on the shipping step, and
on completion. Every checkout line mutation calls `invalidate_checkout`, which
sets `price_expiration` to now, so every one of those paths arrives here with
prices expired. Correcting BEFORE the original runs means our corrections flow
through core's own discount and tax passes exactly as an add-time price does.

### What it does

`reprice()` re-derives, from our own tables and the CURRENT line quantities and
the checkout's CURRENT user:

- every dealer `price_override` (against `best_break` for the line's quantity
  now, clearing the override entirely when no break is reached any more);
- every configured `price_override` (re-running `price_configured` over the
  `wsm.options` snapshot on the line);
- every Compose fee line's `quantity` (parent quantity for a per-unit fee, 1
  otherwise) and `price_override`.

**Whose group.** A checkout with a user on it knows its buyer, and that buyer's
group is the only authority: no stamp promotes a signed-in retail shopper. A
checkout WITHOUT one is the storefront's normal shape, because the key-gated add
resolves the customer server side and never attaches them, so there the group the
add stamped on the line stands. Both dealer lines and configured lines work this
way. Configured lines did not: `_reprice_configured` read only
`tier_group_for(checkout.user)`, so on the anonymous checkout every storefront
actually creates, the FIRST recalculation repriced a dealer's configured line at
retail and rewrote the snapshot to match. Measured on the Stage 2 Kit: 3394.00
became 3494.00, silently, with nothing left on the line to say a tier had ever
applied, and the dealer paid 100.00 more than they were quoted. The stamp is
private metadata, so it is ours to trust; see the stamps section above.

**A configured line that took a tier is a dealer line, and now says so.** MP1 and
MP2 find one by the presence of the `wsm.dealer` key and by nothing else. The
compose add never wrote it, so a SPECIFIC_PRODUCT voucher or a catalogue
promotion came straight off a price that was already the dealer's: 10 percent off
a 3394.00 dealer configuration is 3054.60, both discounts on one line, which
"better of, never both" exists to refuse. The add writes the key when the priced
snapshot says `tier_applied`, and `_mark_dealer` rewrites or clears it on every
recalculation, because a tier can start or stop applying in between (a quantity
change, a group change, the merchant deleting the row).

**Kit member lines are re-derived through the kit, not through the ladder.** A
member line is an ordinary checkout line by design, and its price is not an
ordinary price: it is that member's prorated share of the kit's discount, or a
dealer tier where that is cheaper. MP3 did not re-derive it. A member that took
no tier carried whatever the add stamped on it for the life of the cart, so a
merchant who changed the kit's discount, or a member's list price, went on
selling the old number to every cart already holding one. Measured: a member
added at 9.00 stayed at 9.00 after the merchant doubled the kit discount to 50
percent, where the kit's own arithmetic says 5.00.

*The overcharge.* A member that DID take a tier carries the dealer stamp, and
MP3 read that stamp and re-ran the flat per-variant LADDER over the line,
knowing nothing about the kit it came from. When the merchant then withdrew that
tier, there was no ladder left to find, so the override was cleared outright and
the line fell back to LIST: not to the kit price the shopper was still entitled
to. Measured on the fixture kit: 25.00 on tier, 27.00 entitled, 30.00 charged.
The shopper paid the whole kit discount back on that member without touching
their cart, and nothing on the line said so.

Members are now re-priced together, from the kit, through the same `price_kit`
the add uses, and the lines still in the cart take their unit from that answer.
A shopper who deletes one member keeps the others at their own prices, which is
the kits ruling and not an accident. The kit a line came from, and the kit
quantity it was priced at, are a PRIVATE stamp (`wsm.kit`) written at add time,
for the reason every other pricing input is private: the public copy the
storefront groups the cart on is writable by any unauthenticated caller, and one
that could be forged would hang a retail line off a heavily discounted kit. Cost:
FOUR queries per distinct kit on the checkout (the kit, its members, their
channel prices, and one ladder read covering every member), and zero on a
checkout carrying none.

**Correct and proceed** is the behaviour for drift. **A read never raises**, and
that is the whole of the policy for everything else. This funnel runs on every
price recalculation, and a recalculation is what a cart READ is, so an exception
here does not warn a shopper about one line: `checkout` resolves to `null` and
the cart becomes impossible to render, impossible to repair and impossible to
empty. The shopper cannot even delete the line that caused it, because the
delete mutation returns the checkout.

*The exploit.* A configured item carries its charges as ordinary sibling
checkout lines. Any shopper could point the stock `checkoutLinesDelete` at the
crating-fee line, which every storefront's remove button already calls, and the
next read of that checkout raised `{'lines': ['the charges on a configured item
in this checkout no longer match it']}` with `data.checkout = null`. Not an
undercharge: a self-service denial of service on one's own cart, needing no
tools, no forged input and no account, and leaving support the only exit. The
same wedge was reachable by deleting the configured parent and leaving its fee
behind, and by a merchant deleting an option value a live cart was built on.

So a line this fork can no longer price stops being on the checkout. The row is
deleted and one `logger.warning` names the checkout token, the line pk and the
reason. A charge the shopper was **allowed to decline**, declined the hard way,
is simply declined and the item stays. A **required** charge cannot be declined,
so the configured item goes with it, because leaving the parent is what would
turn the deletion into the undercharge: a crated item sold without the crate. A
fee line whose parent is gone, or which never said what it belonged to, goes on
its own. `Unrepriceable` still exists as this module's internal per-line signal
and is never raised past `reprice()`.

The request that does the dropping has already loaded those lines and resolves a
non-nullable `unitPrice` off them, so they cannot be pulled out of its list
mid-resolve. They are priced at zero instead: that render draws the doomed line
one last time at nothing, which is exactly the total the next read will show, and
the next read does not see the row at all.

### Cost

A checkout holding no wsm-owned line costs **zero queries**: `_classify` decides
from the private metadata already loaded onto `CheckoutLineInfo`. Everything below
is per CHECKOUT, not per line.

A checkout carrying configured lines costs **four**, measured, not estimated: the
option sets on those products, their values, the dealer deltas on those values
(one `prefetch_related`, three queries), and the fees on the same products. It
costs a **fifth** when the checkout carries its buyer, which is the one group
lookup; the storefront's key-gated add resolves the customer server side and
leaves the checkout anonymous, so the common shape is four. An earlier version of
this section said three, which was never true of the code.

A plain dealer line adds **one** ladder query. Each distinct KIT on the checkout
adds **four**: the kit, its members, their channel prices, and one ladder read
covering every member. A `bulk_update` runs only when a number actually moved,
and a second one, by pk, only when a fee line's quantity moved with its parent.
Dropping adds **one** more, and only on the recalculation that finds a line it
cannot price, which is not a state a healthy cart reaches twice. The wrapper
reproduces the original's `price_expiration` early return, so a checkout whose
prices are still fresh costs nothing at all.

That `bulk_update` writes `price_override`, `price_override_reason`, `metadata`
and `private_metadata`, and **not `quantity`**. Core loads the line objects this
wrapper is handed on the REPLICA, so their quantity is whatever that replica last
saw; writing it back turned any price correction into a silent undo of a quantity
the shopper had committed in another request, charging them for one when they
asked for four. The only lines whose quantity this file owns are fee lines, since
a per-unit fee is one line of its parent's quantity, and those go in a second
`bulk_update` by pk that runs only when that number actually moved.

### Verified by

`saleor/wsm/tests/test_reprice.py`, which drives both exploits end to end
through the real endpoints, the real `checkoutLinesUpdate` mutation and
`create_order_from_checkout`, and asserts on the ORDER lines. Every query count
above is an assertion in that file, not prose:
`test_a_checkout_this_fork_does_not_own_costs_no_queries` for the zero and
`test_a_configured_checkout_costs_the_queries_the_doc_says_it_does` for the four
and the five, which asserts the TABLES, so adding a query reddens it by name.
`test_a_dealer_priced_checkout_costs_one_more_query_for_the_whole_cart` pins the
sixth: a checkout carrying dealer-priced configured lines reads the tier ladder
that decides their base once for all of them, never once per line.
