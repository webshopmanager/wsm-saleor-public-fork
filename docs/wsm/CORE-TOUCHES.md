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

Monkey patches: **zero.** Core table edits: **zero.** Our tables carry FKs into
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

## 2. `saleor/settings.py`, +2 lines (U4, 2026-09-08)

```python
    # WSM-FORK: fork-only app, see saleor/wsm/containers/__init__.py and docs/wsm/CORE-TOUCHES.md
    "saleor.wsm.containers",
```

The twin of touch 1, for the same reason: `INSTALLED_APPS` is the only thing an
app cannot do for itself. The label is pinned to `wsm_containers` in `apps.py`
so the three tables carry the `wsm_containers_` prefix and stay out of core's
namespace. Everything else in Containers (models, migration, pricing, views,
admin, tests) lives under `saleor/wsm/containers/`, which upstream does not own.

This is the SHARED touch site: U2 and U3 add their own line in the same block,
so the merge is a two-line conflict resolved by keeping both sides.

Removal cost: delete the two lines and the package. Nothing in core references
Containers.

---

## 3. `saleor/urls.py`, +1 line of code, +1 import (U4, 2026-09-08)

```python
from django.urls import include, re_path
...
    re_path(r"", include("saleor.wsm.urls")),
```

Every URL the fork serves goes behind this one line. The paths themselves live
in `saleor/wsm/urls.py`, which upstream does not own, so U2's compose routes and
U3's dealer routes cost core nothing: the conflict site stays one line however
many endpoints the fork grows. Currently `/wsm/containers/api/checkout/kit-line`.

Also SHARED: whichever of U2, U3 and U4 merges first writes this line, and the
others' diffs collapse into it. `include` joins the existing `django.urls`
import rather than adding a line of its own.

Removal cost: delete the line, the import word, and the `saleor/wsm/` package.

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
  screens appear with no further edit, and the `post_migrate` permission receiver
  in `saleor/wsm/compose/apps.py` needs `wsm_containers` added to its label guard
  so a non-superuser merchant is grantable on them. That one-word follow-up is
  the whole merge cost.
