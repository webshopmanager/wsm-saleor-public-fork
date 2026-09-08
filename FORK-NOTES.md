# FORK-NOTES

Every deviation `webshopmanager/saleor` carries against upstream `saleor/saleor`, and
why. Tracked in the repo on purpose: a table in a pull-request description is
invisible six months later, when the person doing the rebase needs it.

Every file this fork adds or changes carries a `# WSM-FORK:` marker, so the
complete inventory is one command:

```
git grep -l "WSM-FORK" -- saleor/     # every file the fork touches or owns
git grep -n "WSM-FORK" -- saleor/     # and every changed line inside them
```

Inside an upstream-owned file the marker sits on the changed line; a fork-owned
file carries one in its docstring or first line. A cold review pointed out that
the earlier wording promised a "complete inventory" from a grep that surfaced
only the upstream-owned half.

---

## 1. Secondary categories in `Category.products`

WSM 5.0 lets a product live in many categories. Saleor's `Product.category` is a
single FK, so the 5.0 migration keeps one primary category per product and
records the rest in `product_secondary_categories`, a table created and written
by the PartsLogic service and the migration ETL, inside each tenant's own Saleor
database. Saleor did not read it, so a migrated tenant's category pages showed
only the primary members. On the dmk Jeep tree, 11 of 12 categories hold zero
primary products, so those pages rendered empty at HTTP 200.

### Files

| file | owner | change |
|---|---|---|
| `saleor/settings.py` | **upstream** | **+2, and this is the entire upstream footprint.** `"saleor.wsm"` in `INSTALLED_APPS`, and its `# WSM-FORK:` marker. |
| `saleor/wsm/models.py` | fork | `ProductSecondaryCategory` (unmanaged), the availability probe, the SQLSTATE evidence layer, and the env gate. |
| `saleor/wsm/secondary_categories.py` | fork | 312 lines. `EqualsAnyOfArray`, `MultiLegQuerySet`, `with_secondary_categories`, `secondary_categories_active`. |
| `saleor/wsm/category_products.py` | fork | The seam. Swaps `Category.resolve_products` at `ready()`; feature-off delegates to upstream's own function object. |
| `saleor/wsm/apps.py`, `saleor/wsm/migrations/0001_initial.py` | fork | The app config that applies the swap, and a state-only migration that emits no DDL. |
| `saleor/wsm/tests/` | fork | `test_category_secondary_categories.py` (the feature, and the tripwires a rebaser needs: `ALL_PRODUCT_SORT_FIELDS` and its enum-exhaustiveness assertion, the emitted-SQL guard, all the degradation coverage) and `test_patch.py` (the seam's own tripwires). |
| `saleor/graphql/product/types/categories.py` | upstream | **NOT TOUCHED.** Byte-identical to upstream; the resolver is swapped at runtime instead. |
| `saleor/graphql/core/connection.py` | upstream | **NOT TOUCHED.** `MultiLegQuerySet` exists so that it does not have to be. |

The one upstream line is the point. Bill and Matias both hold that we do not
commit on top of saleor, so the feature ships as a Django app: everything lives
under `saleor/wsm/`, and the only thing upstream carries is the app registration
that lets `ready()` run. The complete check is one command, and it is also a test
(`test_patch.py::test_the_fork_touches_exactly_one_upstream_file`):

```
git diff --name-only 0a8ae002..HEAD -- saleor/   # settings.py + saleor/wsm/** only
```

### The seam: how an app changes a resolver it does not own

`WsmConfig.ready()` calls `saleor/wsm/category_products.py::patch()`, which
replaces `Category.resolve_products` with a wrapper. Graphene reads that class
attribute once, when it builds the schema on the first import of
`saleor.graphql.api`. That import is USUALLY after `django.setup()` has run every
`ready()`, but not always: `"saleor.wsm"` is registered last among the local
apps, `saleor.plugins`' `ready()` imports every entry in `settings.PLUGINS`, and
a plugin that imports the schema at module level would build it before this
app's `ready()` runs. In that case the swap would land on a class graphene never
reads again and every category page would quietly revert to primary-only at
HTTP 200. So `patch()` refuses to run if `saleor.graphql.api` is already in
`sys.modules`, turning that silent loss into a boot failure that names the fix
(move `saleor.wsm` earlier, or stop importing the schema at import time).
`patch()` also refuses to stack on another wrapper: it checks
`__code__.co_qualname`, which `functools.wraps` does not copy, so a foreign
wrapper cannot impersonate upstream and break the "feature-off calls upstream's
own function object" promise while looking correct to name-based checks.

The wrapper is deliberately shaped so that **feature-off is upstream by
construction, not by re-implementation**: it computes the probe once, and if the
answer is no it calls the function object it captured from upstream. There is
nothing to keep in sync on that path and nothing to review for parity.

Feature-ON runs a copy of upstream's resolver body with two marked deviations,
which is the cost of this seam and was accepted knowingly. Both deviations sit
inside the resolver's inner closure (skip `qs.filter(category__in=tree)`, apply
`with_secondary_categories` after `filter_connection_queryset`), and nothing
smaller than the resolver encloses them: the only other seams are
`filter_connection_queryset` and `create_connection_slice`, which are global and
shared by every connection in the schema, so patching either would put the fork's
behaviour in the path of code that has nothing to do with categories. A copied
body confined to the ON path is the narrower blast radius. It is pinned by a
source hash so a rebase cannot silently diverge; see hazard 1.

The model lives in a fork-only app rather than in `saleor/product/models.py`
specifically so that `product`'s migration graph is not part of the deviation
set. A `0204_` in `product` would have collided with upstream's next migration
number, and that collision surfaces during `migrate` on a tenant box rather than
as a conflict in a diff.

### Rebase hazards, in order of how quietly they bite

1. **The seam is a runtime swap, so both of its halves fail quietly.** A rebase
   that renames, moves or re-decorates `Category.resolve_products` turns
   `patch()` into a no-op and every category page silently reverts to
   primary-only at HTTP 200; a rebase that merely EDITS the resolver body leaves
   the fork running a stale copy of it, which is quieter still. Two tests in
   `saleor/wsm/tests/test_patch.py` exist for exactly this and are the only
   things that can notice. `test_the_patched_resolver_is_the_one_wired_into_the_schema`
   introspects the built schema rather than the class, so an ordering change that
   builds the schema before `ready()` fails it too.
   `test_the_upstream_resolver_source_is_unchanged` hashes
   `inspect.getsource()` of the captured upstream function against
   `UPSTREAM_RESOLVER_SHA256`. **Re-pin that digest deliberately:** read upstream's
   new body, port the two `WSM-FORK`-marked deviations into
   `_resolve_products_with_secondary`, and only then update the constant. Bumping
   the digest to make CI green is how the copy goes stale.

2. **`MultiLegQuerySet`'s interface is closed on purpose.** It implements exactly
   the queryset methods that `sort_queryset_for_connection` ->
   `connection_from_queryset_slice` and the sorters they reach
   (`ProductOrderField.qs_with_*`, `ProductsQueryset.sort_by_attribute`) call
   today. An earlier version proxied everything through `__getattr__` and wrapped
   whatever came back, which silently wrapped `qs_with_collection`'s
   `aggregate()` dict and made `sortBy: COLLECTION` a request-time `TypeError`.
   If an upstream pull adds a call the class does not implement, it raises
   `AttributeError` and
   `test_every_sort_field_works_with_the_feature_on` fails in CI. **Do not
   "fix" that by reinstating a catch-all `__getattr__`.**
3. **`ALL_PRODUCT_SORT_FIELDS`** in the test file is asserted to equal
   `ProductOrderField`'s members. An upstream pull that adds a sort value fails
   that assertion, which is the intended way to be told to sweep it. The same
   test also asserts the enum **VALUES**, specifically that every sort still ends
   in a unique tiebreak (`slug`, `pk` or `id`), because the per-leg top-k proof
   rests on each ordering being TOTAL rather than on the set of names: an
   upstream pull that drops a sort's tiebreak tail keeps every name and would
   otherwise pass green while returning a wrong row at a page boundary. A mirror
   tripwire pins the category-shaped **filter** surface (`ProductFilter`'s
   `categories` and `has_category`, `ProductWhere`'s `category` and
   `has_category`), so a new category-shaped filter fails here instead of
   widening silently on a live tenant; see "Semantic widenings" below.
4. **`__getitem__` applies the same slice twice, which is only correct because
   the connection never uses an offset.** `connection_from_queryset_slice` always
   slices `[:end_margin]`, never `[start:end]`. A per-leg OFFSET is not the global
   OFFSET, so pushing one down would return the union of each leg's own rows N..M
   and look entirely plausible. It therefore **raises** `NotImplementedError` on
   any slice with a non-zero start, on an integer index, and on a slice carrying a
   `step` (Django answers a strided slice with a plain `list`, so the union would
   otherwise die three frames down as `AttributeError: 'list' object has no
   attribute 'union'`), rather than guessing; a test observes the keys the
   connection actually passes, asserting that every one of them has a start of
   `None` or `0` **and** a step of `None`, rather than asserting it from a reading
   of upstream. So an upstream move to offset or strided pagination fails loudly
   here. This entry used to say the coupling "fails silently", which was
   true before the guard shipped and is no longer.
5. **No `DISTINCT` anywhere in the page path.** `MultiLegQuerySet.__getitem__`
   compiles `leg.values("pk").order_by(<sort key>)[:n]`, and under
   `SELECT DISTINCT` Postgres rejects `ORDER BY` on an expression absent from the
   select list. No product filter or where-filter declares `distinct=True`
   today. `test_emitted_sql_keeps_the_per_leg_limit_and_the_plan_fence` asserts
   the absence of `DISTINCT` for **one** query shape, the one with no `filter` or
   `where` argument, so it catches a `distinct()` that lands on the base path and
   **not** one that arrives with a new filter no test exercises. A `distinct()`
   base does still compile and slice correctly today, which is measured rather
   than assumed. Note the second failure mode: filters run BEFORE the split today,
   but a filter or `where` implementation that called `.distinct()` after
   `with_secondary_categories` would hit the closed interface and raise
   `AttributeError` rather than compiling bad SQL.
6. **`attributeId` is the only non-total sort order, and the per-leg top-k
   argument leans on totality.** `sort_by_attribute_fields()` returns
   `["concatenated_values_order", "concatenated_values", "name"]`, and `name` is
   not unique; every other `ProductOrderField` value terminates in `slug` or `pk`,
   which are. Under a non-total order a leg's `LIMIT` can cut a tied row the global
   order would have surfaced, and the outer query then picks a different row with
   identical sort keys. Upstream's cursor is already unstable for this sort, so
   this is parity rather than a regression, and the binding-scale test with
   distinct attribute values passes in both directions. Worth knowing it is the one
   ordering where the argument depends on the tiebreak.
7. **`= ANY (ARRAY(...))` is load-bearing, not style.** With a plain
   `pk__in=<subquery>`, Postgres pulls the subquery up into a semi-join and,
   unable to estimate the surrounding `visible_in_listings` SubPlan, inverts it
   into a full walk of `product_product_slug_key`. Measured on a 152k-product
   tenant: 1,421 ms against 31 ms, and 12.6 s against 331 ms at 427k products.
   **Do not add a `LIMIT` inside the `ARRAY()`**
   to bound it: the array is an unordered membership SET, not an ordered
   candidate list, so truncating it drops arbitrary members. Measured on the same
   tenant with `LIMIT 101` inside the fence, against the correct row set: 80 of
   101 rows wrong on one leaf category (same count, different products, which is
   the silent-corruption case), 64 of 70 missing on a 443-category tree, 94 of
   101 missing at the root.

### What the fence costs, and how it scales

The `ARRAY()` is a per-statement materialization sized by the psc rows under the
requested tree, **with duplicates** where a product carries rows for several
categories in the same tree. It is not bounded by the page limit; it is bounded by
the catalog. It is built once for the page statement and once for the secondary
`count()` statement, so twice per request, and again for every page of a cursor
walk. **Under `sortBy: COLLECTION` there is a third build**, in the secondary
leg's sentinel aggregate, and that one is unbounded: it carries no `LIMIT`,
because a minimum has to see every row. The `COLLECTION` cost table below already
prices that statement ("two aggregates rather than one"); it is the fence count
that used to read as two on every sort.

Measured on two tenants, the array build isolated from the rest of the statement.
**hdp** is 152,332 products / 82,568 psc rows on a 1-vCPU dev box; **nea** is the
fleet's largest at 427k products, checked to confirm nothing here scales
nonlinearly.

| tenant | tree | psc rows in tree | array build | per id |
|---|---|---|---|---|
| hdp | leaf | 27 | 0.10 ms | 3.7 us |
| hdp | leaf | 354 | 0.18 ms | 0.51 us |
| hdp | 91 categories | 11,426 | 2.75 ms | 0.24 us |
| hdp | 443 categories | 30,381 | 18.2 ms | 0.60 us |
| hdp | root, 1,535 categories | 81,136 | 27.2 ms | 0.34 us |
| nea | worst case at 427k products | - | - | **0.26 us** |

**Nothing in the patch scales nonlinearly**, and the per-id cost at 427k products
is *better* than at 152k, because the fixed costs amortise. Linear in psc rows per
tree is the whole story, which is what makes the week-one secondary-leg duration
logging a growth curve worth watching rather than a cliff to fear.

The fence claim gets stronger at scale, not weaker. At nea's worst case the plain
`IN` shape this replaced costs **12.6 s** against the fence's **331 ms**, a 38x
gap, where the same comparison on hdp was 1,421 ms against 31 ms.

**Do not bound the array with a `LIMIT`.** See rebase hazard 7.

### What the secondary count leg costs, and why it is a range

This paragraph has now been wrong twice, in both directions, so here is the
arithmetic rather than a headline.

The first version said the broad-root count was "+4% on top of upstream". That was
an artifact of hdp's pathological 2.2 s upstream baseline on a 1-vCPU box, not a
property of this code, and it made the cost look negligible everywhere. The second
version replaced it with "about 10.8 microseconds per psc row", presented as
tenant-independent. **That number is not derivable from the measurements it cited
and is roughly a factor of ten off on one of them.** A cold review caught it by
doing the division.

The measurements, with the division shown:

| tenant | psc rows surviving the exclude | upstream root count | with the union | add | per row |
|---|---|---|---|---|---|
| hdp (1 vCPU, pathological upstream plan) | 81,136 | 2,173 ms | 2,263 ms | 90 ms | 90,000 / 81,136 = **1.11 us** |
| hdp, the secondary count leg measured directly | 81,136 | - | - | 88.1 ms | 88,100 / 81,136 = **1.09 us** |
| nea synthetic (healthy plan) | 36,848 | 645 ms | 1,163 ms | 518 ms | 518,000 / 36,848 = **14.06 us** |

**So the per-row cost is not a constant. It spans roughly 1 to 14 microseconds per
surviving row across the tenants and plan shapes measured, a factor of 13.** The
cause is plan shape, not row count: hdp's baseline is already walking the table, so
the extra leg rides along cheaply, while nea's healthy plan pays for the leg
outright. Neither is "the" number.

How to size a tenant with this: take the psc rows that survive the exclude under
the tree you care about, and expect an add between roughly **1 us and 14 us per
row**, landing near the top of that range on a tenant whose upstream plan is
healthy. On nea's 36,848 rows that is the measured 518 ms, and the root count goes
from 645 ms to 1,163 ms, so it **roughly doubles**. On hdp it is 90 ms on top of
2,173 ms. Both are real; the range between them is the honest answer, and a single
figure would be a third revision of the same mistake.

**The honest worst-case scale number, stated rather than left to be discovered.**
nea is the fleet's largest tenant (427k products) and its psc table was measured
only at a synthetic 36,848 rows, a psc-to-product ratio of 0.086. hdp's real ratio
is 82,568 / 152,332 = 0.54, six times denser, and dmk's Jeep tree is 100%
secondary. If nea's real psc load lands near hdp's density, a broad root tree
carries roughly 427,000 x 0.54 = 231,000 surviving rows, and at the top of the
range above that is 231,000 x 14.06 us = **~3.25 s** added to one root
`totalCount`, on top of upstream's own count. This is arithmetic on the numbers in
this file, not a measurement; it is here because `totalCount` is on the SSR PLP
path and TTFB feeds LCP. It is a reason to sequence the rollout smallest-tree-first
and to re-measure nea at real density before it goes anywhere near production, not
a reason to abandon the shape. The count leg, not the fence, is the dominant added
cost everywhere in the data.

**Two sorts are unmeasured for the per-leg-`LIMIT` short-circuit claim.** Every
page number in this file is `ORDER BY slug`, an indexed total order where a leg's
`LIMIT` short-circuits. `PRICE` and `MINIMAL_PRICE` annotate a `Min()` aggregate,
so Django adds a `GROUP BY` and the leg must aggregate every candidate row before
it can order and cut: the short-circuit does not apply, and `TYPE` orders by a
join column an index cannot serve either. Those shapes are standard PLP furniture
and have no number here. Re-run the five-category hdp page table under
`sortBy: PRICE` and `sortBy: TYPE` before rollout; that is a pre-rollout
measurement item, not a code change.

### The `COLLECTION` sort's sentinel, and why it is tree-scoped

`ProductOrderField.qs_with_collection` needs a sentinel
(`Min(collectionproduct__sort_order) - 1`) that every product with no explicit
collection ordering sorts below, and it asks the queryset for it. Two wrong
answers were shipped and caught by review before the third:

- **one leg** is wrong for ORDERING. A secondary-leg product ordered below
  everything in the primary leg leaves the sentinel stranded mid-page.
- **`base`** is right for ordering (it is a superset, so its minimum is at or
  below the union's) but wrong for COST and for CURSOR STABILITY: `base` carries
  no category restriction, so it aggregates over every visible product in the
  catalog where upstream aggregated over the requested tree, and the sentinel is
  embedded in the `COLLECTION` cursor, so a `sort_order` change anywhere in the
  catalog could invalidate a cursor minted for one category page.

Shipped: the minimum of the legs' minima. Minima combine, both legs are
tree-scoped, so the scope and the cursor's blast radius are back to upstream's and
the cost is two tree-scoped aggregates instead of one. `aggregate()` raises
`NotImplementedError` for anything but `Min`, because nothing else combines that
way and a plausible wrong number is worse than a failing test.

Measured on the 152k-product tenant, one aggregate:

| tree | over the tree (upstream's scope, and ours) | over the whole catalog (the rejected shape) |
|---|---|---|
| leaf, 8 primary products | **0.46 ms** | 1,200 ms |
| 443 categories, 4,648 products | **357 ms** | 1,202 ms |
| root, 26,622 products | **1,585 ms** | 1,204 ms |

So the fork pays 2x upstream for this sort (two aggregates rather than one), and
the rejected catalog-wide shape was 2,600x worse than upstream on a leaf, which is
the common case. **The root row goes the other way and is not hidden here: at the
root the rejected catalog-wide shape is cheaper, 1,204 ms against 1,585 ms**,
because a 1,535-category tree predicate is pure overhead on a scan that was
already touching most of the catalog. The leaf distribution is what decides it. `COLLECTION` on
`Category.products` is off-label anyway (the schema documents it as
Collection-only) and nothing on the storefront requests it; these numbers exist so
the choice is a decision rather than an accident.

### Assumptions about the tenant database

- **A leading-`category_id` index on `product_secondary_categories`.** PartsLogic
  creates `idx_psc_category_id`, and the `ARRAY()` fence reads the tree's slice
  through it. The probe deliberately does **not** require it: absence is a
  performance property, not a correctness one, and turning the feature off would
  trade a slower page for a wrong one.

  Measured cost of its absence on a 152k-product / 82,568-psc-row tenant (12 MB
  / 1,497 pages of psc), by forcing the planner off every index path for the psc
  slice the `ARRAY()` fence builds:

  | tree | with `idx_psc_category_id` | with no psc index |
  |---|---|---|
  | leaf, 27 psc rows in tree | 0.14 ms (Index Only Scan) | 21.8 ms (Seq Scan) |
  | leaf, 354 psc rows in tree | 0.23 ms (Index Only Scan) | 22.2 ms (Seq Scan) |
  | root, 81,136 psc rows in tree | 36.8 ms (Seq Scan already) | 36.9 ms |

  So a leaf category page would go from 2.6 ms to roughly 24 ms, and broad trees
  would not change at all because the planner already prefers a sequential read
  there. Slower, bounded by the table's size, and never wrong. The add scales
  with psc size, so revisit if a tenant's psc table reaches millions of rows.
- **Ids are Saleor ids.** The 5.0 to 6.0 load inserts explicit primary keys, so
  psc's `product_id` and `category_id` are in Saleor's id space. The table's own
  foreign keys make a *dangling* id impossible; they do not make a
  *wrong-but-valid* one impossible. A tenant loaded without explicit ids would
  break this silently.
- **One Saleor process per tenant.** The probe cache is keyed on the Django
  connection alias, which identifies a database only under that assumption. It
  would be the wrong key under a shared-process router or schema-per-tenant.

### Degradation

Two independent mechanisms, because the first one alone is not enough and a cold
review proved it.

**The probe.** `secondary_categories_available()` requires the table to exist, to
be readable by the runtime role, and to hold at least one row, re-probed every
`PROBE_TTL_SECONDS` (60). That handles the slow transitions in both directions: a
tenant that gains its first row starts being served correctly without a Saleor
restart, and a tenant that loses the table stops naming it within an interval.

The probe costs, per interval per alias, **in autocommit** (the storefront read
path): **one** statement when the table is absent, since it short-circuits on
`to_regclass`, and **three** when the table exists, the third being the population
check. **That is per alias, and the fleet runs two.** `Category.products`
resolves on `replica` for reads and on `default` inside a mutation payload, so a
real deployment probes both and pays the figure twice per interval. The aliases
also degrade independently, the cache being keyed on the alias: a proven table
loss observed on `replica` does not mark `default` unavailable, and the writer
path degrades on its own first failure instead. Present-but-empty is the common fleet state, not an edge: Truck Outlaw,
Body Kits, Steve White, Shifted, Caltric and Camlocker are all in it.

**Inside a transaction block, add two**, because `recoverable_failure` takes a
savepoint there: so 3 and 5 rather than 1 and 3. The in-a-block case is the
`Category.products` edges selection in a mutation payload. The 1, the 3 and the
in-a-block 3 are each pinned by a test; the in-a-block 5 is that savepoint pair
added to the pinned 3 and is not separately pinned. An earlier revision quoted
only the autocommit figure and claimed all four were pinned; neither was right.

The probe's own `except DatabaseError` is deliberately broad, because these are
statements the fork ADDED: a transient failure in one of them must never turn into
a 500 upstream would not have had. It reports unavailable, logs at warning, and
the TTL heals it. It runs inside `recoverable_failure` for the reason below.

**The read path, because a cached answer can be wrong RIGHT NOW.** PartsLogic's Go
migrator creates, drops, renames and rebuilds this table while Saleor is running,
grants change independently of Saleor's lifecycle, and a failover target may never
have received the DDL. Trusting the probe means `Internal Server Error` on every
category page for the rest of the interval: the whole storefront PLP surface plus
the dashboard's category product list.

Degrading on that is easy to get dangerously wrong, and the first attempt was: a
`try` around the whole slice caught `ProgrammingError` from anything, including
the channel lookup and the sorter builds, so an unrelated failure became a
silently truncated page at HTTP 200 with the real error never reaching a log. That
is the same soft-404 pathology this feature exists to remove. Three layers now
scope it, and each is independently pinned by a test:

1. **Structural.** The guard wraps exactly the statements that name the
   secondary table: the merged page query, the SECONDARY-leg counts, or the
   secondary leg's `COLLECTION` sentinel aggregate.
   `MultiLegQuerySet.__getitem__` executes eagerly for that reason. Upstream's own
   statements are outside it (the primary-leg count, the primary leg's sentinel,
   the channel lookup, the sorter builds), so those failures propagate as
   upstream's would. A test asserts this in **both** directions, and the second
   direction is why: an earlier revision left the `COLLECTION` sentinel outside
   the guard on the argument that it "runs before any slice", which was true and
   irrelevant, because the fork's version of that statement names the secondary
   table. A genuine table loss under that one sort was then a hard 500, repeating
   for the whole TTL because nothing flipped the probe. The forward assertion
   ("everything inside the guard names the table") stayed green throughout.
2. **Evidence.** The SQLSTATE has to say the table itself is the problem
   (`TABLE_UNAVAILABLE_SQLSTATES`: 42P01 dropped or renamed, 42501 grant revoked,
   3F000 schema swapped, 42703 a column dropped or renamed, 42883 a column's
   TYPE changed so `bigint = <newtype>` resolves no operator; the last is what a
   "rebuilt with a different shape" event actually raises, and without it such a
   rebuild 500s every category page indefinitely because the probe never checks
   types). 42804 is deliberately excluded: no column rebuild raises it (measured
   over 19 types on both columns), while a UNION operand mismatch does, and the
   guarded page statement is the fork's only UNION with a corroborator that has
   none, so accepting it would degrade a broken union to a wrong page at 200. A
   `ProgrammingError` meaning anything else, or carrying no SQLSTATE at all,
   propagates untouched. `OperationalError` (a deadlock) is not caught here at
   all: a transient failure is not an availability verdict. For the two states
   whose message names the relation (42P01, 42501) that name has to be **ours,
   exactly**: parsed out of `diag.message_primary` with an anchored pattern and
   compared for equality, never tested for containment. Containment is evaded two
   ways by real errors, both demonstrated: `product_secondary_categories_v2`,
   `_old`, `_backup` and `wsm_product_secondary_categories` all contain our name,
   and those are precisely the names PartsLogic's rename-and-swap rebuild
   produces; and Postgres appends a `LINE n:` echo of the failing statement, which
   is by construction a statement naming our table, so an error about a completely
   different relation carries our name in its own echo. `message_primary` is the
   server's first line and contains neither. Note that `diag.table_name` is
   **empty** for both states on Postgres 16 with psycopg 3.2.9, measured, so the
   structured field is not an option and the pattern is the answer.
3. **Corroboration.** The primary-only fallback runs BEFORE the feature is marked
   unavailable. It shares everything with the union except the secondary
   predicate, so a failure rooted elsewhere (the classic being code deployed ahead
   of `saleor-migrate.service`, which raises 42703 for a column that does not
   exist yet) reproduces in the fallback and surfaces, and the probe cache is
   left alone.

Every degradation is logged at warning. Without that it would be invisible: the
fallback compiles different SQL and usually succeeds, so nothing else in the stack
would ever report it.

**Savepoints.** A failed statement aborts the surrounding transaction block, so
both the fallback and the probe's swallowed failure would otherwise leave the
connection dead for the rest of the request. Reads run in autocommit
(`ATOMIC_REQUESTS` is unset), where there is no block to abort, so
`recoverable_failure` takes a savepoint **only** when the connection is already
inside one. The hot path pays zero extra statements. `SAVEPOINT` is itself a
statement that `restrict_writer_middleware` refuses on the writer alias, so the
bookkeeping (and only the bookkeeping, never the queries) runs inside
`allow_writer()`.

The live in-a-block case is a `Category.products` **edges** selection inside a
mutation payload; there is a test for it, including that the mutation's own write
survives the savepoint rollback. `totalCount` inside a mutation payload does not
work at all, **upstream included**: `create_connection_slice` returns it as a
callable graphene resolves after the resolver has returned, therefore outside
`allow_writer_in_context`, and on the writer alias that trips `restrict_writer`.
A parametrised test pins that as inherited behaviour rather than a fork
regression. Note also that `restrict_writer_middleware` is installed by the test
settings; production installs the logging variant only when
`ENABLE_RESTRICT_WRITER_MIDDLEWARE` is set.

**One inconsistency worth knowing before you see it in a log, stated sharply.** If
the page statement succeeds and the table becomes unreadable before graphene
resolves `totalCount` (it is a callable resolved after the resolver returns), the
two halves land in **one response body**: union `edges` beside a primary-only
`totalCount`. So `totalCount` can be **strictly smaller than the number of edges
handed over with it** in the same payload, not merely inconsistent with a later
request. A test pins the exact case: 16 edges with `totalCount` 8.

Any consumer deriving a page count, an "N results" label or an infinite-scroll
stop condition from `totalCount` will act on a number smaller than the data it
already has.

**And one more shape of the same bounded window, worth saying plainly.** On a
tenant whose tree is mostly secondary-only, a degradation renders as an empty
grid at HTTP 200, which is exactly the soft 404 this feature exists to remove: on
dmk's Jeep tree 11 of 12 categories hold zero primary products, so for one TTL
those pages are indistinguishable from the bug. That is the page fallback
behaving as specified (`list(self.legs[0][key])` is what upstream would have
returned) and it is not a defect in the fallback. Whether an empty primary leg
plus a degradation should surface as an error instead is a deploy-time policy
question, not a code change: it trades a bounded wrong-looking page for a bounded
hard failure, and which is preferable depends on the tenant. Bounded to one TTL, because the failure flips the probe and the next
request skips the leg entirely, so both halves agree again. Closing it properly
needs atomicity across a boundary upstream owns, which is why it is documented
rather than fixed. **If the dashboard ever divides by `totalCount`, this stops
being cosmetic.**

When the probe is false the wrapper hands the request to **upstream's own
resolver function object**, so the emitted SQL is unchanged by construction rather
than merely equivalent. Verified three ways: the wrapper's OFF branch is asserted
to call that object and no copy of it
(`test_patch.py::test_the_feature_off_path_calls_upstreams_own_function`), the
compiled SQL was captured for seven query shapes against upstream (identical, bar
the probe), and on a second tenant whose psc table is present but empty the served
SQL matched the unpatched build exactly.

### Turning it off, and the one way it turns itself off silently

Three levers, fastest first. None of them needs a code change.

1. **`WSM_SECONDARY_CATEGORIES=off` on the box** (`off`, `false` or `0`, any case).
   Read once at import in `saleor/wsm/models.py` and checked before the probe, so
   a disabled box issues upstream's SQL and never touches the table at all. It
   takes a process restart to apply and it is the per-tenant rollout control:
   ship the image everywhere, turn it on where you want it.
2. **`REVOKE SELECT ON product_secondary_categories FROM <runtime role>;`** This is
   the emergency kill and it needs **no deploy and no restart**. The probe's
   `has_table_privilege` check fails, so the feature is off everywhere on that
   tenant **within one `PROBE_TTL_SECONDS` (60 s)**, and any request already
   in-flight against the old cached answer degrades through the 42501 path rather
   than erroring. `GRANT` it back and the feature returns on the same interval.
   This lever exists because the probe was built to answer the privilege question,
   not because anyone designed a kill switch; it is documented here so it is found
   during an incident instead of after one.
3. **Emptying the table** turns the feature off too, which is the hazard below.

**Truncate-and-reload is a silent, clean feature-off for the duration of the
load.** The probe's population check reads `EXISTS (SELECT 1 FROM ...)`, so while
PartsLogic's ETL holds the table empty every category page quietly serves the
primary-only answer at HTTP 200, with no log line and no degradation path taken,
because from Saleor's side nothing is wrong: on a mostly-secondary tree that is a
window of empty grids the storefront may noindex. The durable fix is on the
PartsLogic side, a staging table plus an atomic rename (or a rebuild inside one
transaction) so the empty state is never observable, and it is tracked separately
rather than worked around here; a Saleor-side workaround would cost every request
forever to paper over a window that the writer can close for free.

**The deploy is image-only.** `saleor/wsm/migrations/0001_initial.py` is state-only
(`managed = False`), so it emits no DDL and there is nothing for
`saleor-migrate.service` to do for this feature: rolling the patched image is the
whole deploy. The table itself is created and owned by PartsLogic, outside
Saleor's migration graph, which is also why arriving out of order is safe.

### Semantic widenings, on purpose: there are THREE

With the feature on, `filter_connection_queryset` receives a base that is not yet
restricted to the tree, so **every category-shaped filter widens**, not just the
one that first got a test. An earlier revision of this file described the set as a
singleton; it is not, and the cause being structural means the list is the whole
category-shaped filter surface:

1. **`filter: {categories: [X]}`.** A product primary in X, outside the tree, that
   reaches the tree only through a secondary row is returned, where upstream
   returned nothing.
2. **`where: {category: {eq | oneOf}}`.** The same widening through a different
   API surface. This is the one the **dashboard** uses.
3. **`filter: {hasCategory: false}` and `where: {hasCategory: false}`.** Upstream
   restricted the base to the tree first, so this was structurally empty on
   `Category.products`. With the union on it returns products with no primary
   category at all that reach the tree through a secondary row. Arguably this
   becomes *more* correct rather than merely wider.

All three are the feature working as intended, and all three now have tests. The
finding they came from was the claim, not the behaviour.

The **top-level** `products(filter: {categories: [...]})` query is unaffected and
stays primary-only; see below.

### Deliberately unchanged

Each for a reason, so that the inconsistency is a decision rather than an
oversight:

- `Product.category` stays the single primary home, so breadcrumbs, canonical
  URLs, the dashboard's category field and the delete cascade keep reading the FK.
- `collect_categories_tree_products()` stays primary-only: deleting a category
  must not unpublish products that merely referenced it as a secondary.
- `attributes(inCategory:)` and top-level `products(filter: {categories:})` stay
  primary-only. Widening them buys nothing on the storefront PLP path.
- `CategorySortField.qs_with_product_count` counts primary products only, so a
  dashboard category list can show a count that disagrees with the category's own
  product list. Known; the fix belongs with whoever owns that screen.
- `product_category_priority` carries a per-category ordering PartsLogic honors
  and Saleor's `sortBy` knows nothing about.
