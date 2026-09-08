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

## 3. `saleor/urls.py`, +1 line of code

```python
    re_path(r"", include("saleor.wsm.urls")),
```

Every URL the fork serves goes behind this one line. The paths themselves live
in `saleor/wsm/urls.py`, which upstream does not own, so U3's dealer routes and
U4's container routes cost core nothing: the conflict site stays one line
however many endpoints the fork grows. Currently `/wsm/compose/*` (the two
storefront endpoints), `/admin/` (the merchant UI, on its OWN `AdminSite` so
that no other app's ModelAdmin can land on it) and `/static/` (the admin's CSS,
because this box runs `DEBUG=False` with no web server in front of uvicorn and
whitenoise is not a Saleor dependency).

Removal cost: delete the line and the `saleor/wsm/` package.
