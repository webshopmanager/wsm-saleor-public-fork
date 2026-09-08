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
