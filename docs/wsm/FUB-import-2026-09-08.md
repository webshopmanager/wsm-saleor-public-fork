# Fuel Lab's real option sets on the bake-off (U6), 2026-09-08

Fuel Lab's live 5.0 configurator now runs on the 3.23.31 fork against Fuel Lab's
own catalog, priced out of Compose's own tables. Nothing was hand-entered and no
number below was tuned to fit: every figure is a count of rows that were read or
written by the commands recorded here.

## What the data is, and where

- **Catalog**: Fuel Lab is 315 products in channel `fuelab` inside the bake-off
  copy `saleor_bakeoff_fub` on wsm-devops. There is no separate Fuel Lab Saleor
  database on pl-sandbox; the sandbox keeps Fuel Lab as a second channel in the
  Tonneau Outlaw database, so the copy was taken from there. Saleor product id is
  `800000 + <5.0 product id>` and the base variant's SKU is the 5.0 stock number,
  which is what the import matches on.
- **Configurator**: WSM 5.0 site 1848 (`fuelab.com`) in `wsm_live_fub`, read
  read-only through the `dbmain-prod-wdsolutions-1` reader.
- **Server**: `serve-fub.sh` on 127.0.0.1:8025, started for the proofs and killed
  after. The 8020 bake-off service and its database were not touched.

## The two shapes in the 5.0 data

Everything the import does follows from two facts measured before it was written.

1. **A 5.0 set is shared; a Compose set is not.** Fuel Lab binds zero sets
   directly to a product: all 126 bindings run through `product_option_link`.
   111 distinct sets cover 97 products, and the five QSST axes are each linked to
   four products. Compose hangs a set off one product so a PDP reads one
   product's rows in one query, so the link expansion happens at import time:
   126 links become 126 `OptionSet` rows over 111 distinct sources.
2. **A dealer tier is a duplicate value row.** 5.0 has no per-option-value tier
   table. It repeats the value once per buyer group and names the group in
   `desc`: `("Red", "Retail", 34.00)`, `("Red", "Dealer 1", 32.30)`, `("Red",
   "Dealer 2", 21.97)`. The Retail row becomes the `OptionValue`; each sibling
   naming a price group becomes one `DealerTierOptionPrice`. Measured on the
   whole site first: `(set, name, sku)` is unique among retail rows, unique among
   each group's rows, and every one of the 1,148 dealer rows has a retail sibling,
   so the natural key needs no new column and no schema change.

An option value is never a Saleor variant. The catalog still carries 607 shadow
variants (`FMBG-62810-0:490-7-20815` and friends) from an older import that made
one; matching on the exact stock number leaves them alone, which is what
`test_tier_price_lands_on_the_base_variant_not_the_shadow` holds in place.

## Counts

Read from 5.0 (site 1848): 126 set links, 1,741 option values, 4 customer groups,
0 product fees, 626 tiered product prices.

| Written | Run 1 | Run 2 |
|---|---|---|
| `OptionSet` | 126 created | 126 unchanged |
| `OptionValue` | 607 created | 607 unchanged |
| `DealerTierOptionPrice` | 1,062 created | 1,062 unchanged |
| `DealerGroup` | 3 created | 3 unchanged |
| `TierPrice` (product-level dealer price) | 626 created | 626 unchanged |
| `Fee` | 0 (Fuel Lab has none) | 0 |
| `compose.configurable` metafield | 97 products written | 97 unchanged |
| unmatched SKUs / unknown `desc` / orphaned tiers / stale values | 0 | 0 |

607 values and 111 distinct sets is exact parity with the 2026-08-30 census.

Two of those numbers look wrong at a glance and are not:

- **607 values against 593 retail rows in 5.0.** The five QSST sets are linked to
  four products each, so their values are written once per product. 593 + 14 = 607.
- **1,062 tier rows against 1,148 dealer rows in 5.0.** 11 of the site's 122 sets
  are linked to no product at all. Their rows are read and then have nowhere to
  land. The import counts what it wrote; it does not invent a product to hold them.

Fuel Lab has 3 active price groups (`dealer-1`, `dealer-2`, `dealer-3`) and only
two of them price option values, so `dealer-3` is created with no deltas under it.
No 5.0 set on this site is hidden, so `--include-hidden` changed nothing here.

## Running it

```bash
MYSQL_PWD=... /home/ubuntu/bakeoff/run-fub.sh import_option_sets_50 \
  --host dbmain-prod-wdsolutions-1.cluster-ro-cpum6qcicyxr.us-west-2.rds.amazonaws.com \
  --user <reader> --database wsm_live_fub --site-id 1848 \
  --channel fuelab --image-base https://fuelab.com/images
```

`--dry-run` reports the same counts and rolls back. The password is only ever read
into `MYSQL_PWD`, so it is not in argv and not in anyone's `ps`.

`--image-base` builds `https://fuelab.com/images/F<image id>.<extension>`, which is
the live shape: 5.0 leaves `image.url` empty and derives the path from the id.
`https://fuelab.com/images/F198651229.jpg` returns `200 image/jpeg` today. 526 of
the 593 retail values carry an image.

## The proofs

All five ran against the live server on 8025; the script starts it, proves, and
kills it (`_scratch/proof-fub.sh`, `_scratch/proof-fub.py`).

**(b) The QSST configurator over the API.** `GET
/wsm/compose/api/storefront/products/saleor/<gid>/option-sets` on product 800306
returns 5 required single-choice axes: Lift Pump (3 values), Surge Tank Pump (8),
Fuel Filter Neck (4), Fuel Level Sensor (2), Fuel Cell Vent Kit (2).

**(c) The configured price on the real QSST product.** Base 893.00 plus the Twin
Screw 20815 surge pump (1,550.00) plus the FUELAB 500LPH lift pump (500.00), the
other three axes on the merchant's zero-cost default:

```
POST /wsm/compose/api/checkout/configured-line   ->  200
unitPrice     2943.00
compositeSku  FMBG-62810-0-20815-49614
```

**(d) A dealer pays the tier price.** The QSST axes carry no dealer deltas in 5.0,
so this is proved on `FMBG-40401` (product 800001, base 649.00), whose Color axis
does. A real Fuel Lab Dealer 1 customer (`loretta@css-racing.com`) was linked to
the imported `dealer-1` group:

```
Red quoted at retail     34.00      unitPrice retail    683.00
Red quoted to the dealer 32.30      unitPrice dealer-1  681.30
```

**(a) and idempotency.** Both runs are in the counts table above: the second run
created and updated nothing, in every category.

**(e) The suite.** `pytest-fub.sh saleor/wsm` green in `wt-fub`, including the
core-tables guard. The import adds no model and no migration; `git status` after
the run shows only the command and its tests.

## What the import will not do

- It never creates a Saleor product, variant or channel listing. A 5.0 set whose
  stock number is not in the channel is counted in `sets_unmatched` and named in
  `unmatched_skus`.
- A `desc` naming no price group is left alone and reported as
  `values_desc_not_a_group`, never guessed into a tier.
- A hidden 5.0 set stays off the storefront unless `--include-hidden` asks for it.
- A 5.0 prompt type with no Compose equivalent stops the import rather than
  defaulting to a dropdown: guessing the prompt is guessing what the shopper is asked.
- A rename in 5.0 reads as a new row and leaves the old one behind. Those are
  counted as `values_stale` rather than deleted, because deleting a merchant's
  live configurator on the strength of a rename is the worse failure. Deleting
  them is the follow-up, and it needs a merchant-visible confirmation step.
