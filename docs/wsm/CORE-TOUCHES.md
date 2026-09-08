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

Monkey patches: **one** (MP1 below, added by U3; the design doc budgeted
zero, see that entry for why the stock levers do not exist). Core table edits: **zero.** Our tables carry FKs into
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

## 4. `saleor/urls.py`, +1 line of code (U2 and U3 together, 2026-09-08)

```python
    re_path(r"", include("saleor.wsm.urls")),
```

**Shared touch.** U2 (Compose endpoints, the merchant admin, static) and U3
(Dealer endpoints) each need the fork's URLs mounted, and both branches wrote
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

Known limit, deliberate: this covers LINE-level discounts (catalogue promotions,
`SPECIFIC_PRODUCT` and `apply_once_per_order` vouchers). An ENTIRE_ORDER voucher
is a checkout-level discount in Saleor and still reduces the order total that a
dealer line contributes to. Filed rather than fixed here because the fix belongs
with the order-level discount distribution, which U4's kit money spec also
touches.
