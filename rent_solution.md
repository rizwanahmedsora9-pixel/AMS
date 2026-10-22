# RENT SOLUTION — plan (discussion / design only, **no code change**)

> Status: **PLAN ONLY.** Nothing in this document has been implemented.
> No `.py`, `.html`, model, migration or template has been modified to produce it.
> Branch for the eventual work: `arena/029e64b4-ams`.
> Evidence base: the live code in this repo + a **read-only** inspection of
> `LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb` (production snapshot exported
> 2026-10-20 from `rehmanahmed.pythonanywhere.com`, 39,269 rows, `integrity ok`).

---

## 1. The business, as AMS must model it

1. We **sell material** (cash / credit / open khata) — direct sale.
2. We **book** material for a client first (booking = reserved quantity + reserved
   rate + the client's receivable), then **deliver** against it (`Booking Delivery`,
   amount 0 — the money was already booked).
3. On every bill (booking **and** sale) we also **charge the client a rent**
   (delivery/freight charge) so we can recover what we pay out.
4. We **pay our delivery persons** that rent, per trip, on the basis of those
   same records.
5. We give **discounts** on the bill, and the client settles the **whole bill**
   (material + rent) through payments.

So rent is: **a charge on a bill (in) and a payable to a driver (out)** — a
pass-through service margin. It is **not** a bag of anything.

---

## 2. How AMS models rent today (verified in code)

| Piece | Where | Nature |
|---|---|---|
| Rent **charged to client** | `Material` rows `RENT-CEMENT`, `RENT-STEEL`, `RENT-GENERAL`, `RENT STEEL` (+ `LABOUR`, `CHAUGATH-LABOUR`) used as priced lines on `BookingItem` / `DirectSaleItem` | **inventory item** ← the problem |
| "Is this line rent?" | `app/services/sales_core.py:572 _is_rent_material_name()` → `'rent' in name` <br> `templates/direct_sales.html:1500 isRentMaterial()` → `txt.includes('rent')` | **name heuristic**, duplicated in Python + JS |
| Rent revenue / cost / loss | `DirectSale.rent_item_revenue`, `.delivery_rent_cost`, `.rent_variance_loss` (`models/sales.py`) | derived aggregates |
| Rent **owed to driver** | `SaleDeliveryPerson(bags_delivered, rent_amount)` + legacy mirror `DeliveryRent` (`models/delivery.py`) | correct, non-inventory |
| Paying the driver | `DeliveryPersonPayment` → `AccountTransaction('Driver Payment')`, FIFO settle + waive-off (`app/services/driver_payments.py`, `/delivery_rents`, `/delivery_person_ledger/<id>/pay`) | **already built and correct** |
| Equipment rent by day (different business) | `models/rentals.py` `FBMRentalItem.rent_per_day`, `FBMRental.days_used()` | dormant: models + wipe targets only, **no routes/UI** |

### Why a rent line *feels* like it needs a GRN

```
material must exist in the master   →  app/services/lookups.py:149 resolve_transaction_material()
                                        "typed text is never used to create a Material"
every chargeable line writes OUT     →  _direct_sales_add_direct_sale.py (item loop) → Entry(type='OUT')
Material.total is recomputed         →  app/services/void_rebuild.py:1876 _rebuild_material_totals()
                                        (totals are Σ IN − Σ OUT over entry rows)
the only blocker                     →  _direct_sales_add_direct_sale.py:376-383
                                        "Insufficient stock for X … Enable 'Allow Negative
                                         Stock' or global setting to bypass."
GRN per line                         →  OPTIONAL ("GRN (optional)", templates/direct_sales.html:683)
```

**A GRN is not required by the app.** Production survives only because
`settings.allow_global_negative_stock = 1`, which is why the snapshot contains
**zero** GRN lines on any RENT material and **zero** GRNs whose supplier is a
delivery person. The pressure to invent a "rent GRN from the driver" appears the
moment that switch is off.

### Why "GRN with the delivery person as supplier" is wrong, mechanically

| Effect | Where |
|---|---|
| typing an unknown supplier **auto-creates a `Supplier` master row** | `app/blueprints/ops/grn.py:44-50` |
| stock "received" that never existed, shown as GRN receipt history | `grn.py:225-236`, `blueprints/inventory/stock.py::_grn_receipt_history` |
| `GRNItem.price_at_time` becomes the **cost of rent**, then FIFO-consumed and frozen as `cost_rate_at_sale` | `app/services/sales_core.py:233-292`, `_profit_profit_reports.py:378-388` |
| a `SupplierPayment` + real cash movement is posted, moving the driver into **payables** | `app/services/grn_svc.py:136 _sync_grn_auto_supplier_payment()` |
| the driver now has a vendor ledger that must be cleaned with the shift/hard-delete machinery | README "Supplier ledger shift", `app/services/supplier_ledger_shift.py` |

A supplier sells goods you buy and consume. A driver is a **payee**. Never bridge
them through `GRN`/`Supplier`.

### What the design is costing you today (snapshot numbers)

- Rent charged to clients: **Rs 2,158,698** (1,607 rent lines on 1,533 of 3,364 active sales) **plus** rent booked in bookings (`RENT-CEMENT` alone: 31,952 units / Rs 1,416,995).
- Driver rent owed: **Rs 1,447,815** on 1,070 sales (3,756 `SaleDeliveryPerson` rows).
- **1,807 active sales paid a driver with no client rent charge**; **484 charged rent with no driver cost** → the two sides are not linked by anything.
- Booked rent 31,952 vs delivered rent 32,076 → the rent pool **drifts** from the material pool because they are two separately typed lines.
- Stock of the pseudo-materials: `RENT-STEEL −84,090.6`, `RENT-CEMENT −32,076`, `RENT STEEL −22,707.89`, `RENT-GENERAL −3,438`, `CHAUGATH-LABOUR −505`, `LABOUR −215`, `MISC −63`, `BACK FILLING −1`.
- `DeliveryPersonPayment` rows: **0**. `AccountTransaction` type `Expense`: **2 rows, Rs 1,420**. ⇒ all driver money leaves the shop **outside** AMS ⇒ cash/balances and daily reconciliation are structurally overstated.
- Recorded `rent_variance_loss` total: **Rs 18,109 on 8 sales** — cannot be true while 1,807 trips were unbilled; and `rent_item_revenue` re-counts booked rent at the booking rate although the P&L already recognises it on the booking line (`_profit_profit_reports.py:347` skips Booking-Delivery lines). ⇒ the "Rent Difference" figure is directional, not auditable.
- 4 rent lines already carry a non-zero `cost_rate_at_sale` (rent acquired a fake purchase cost).
- 70 materials exist; **only 14** were ever received through a GRN; **50** are sold/booked with **zero** GRN receipts. This is a *GRN-discipline* gap of which rent is only the loudest symptom.
- Chart of accounts: all 13 `Account` rows are cash/bank (+1 loan). `blueprints/accounts/classification.py` **already defines** `Income → Service Income`, `Expenses → Delivery & Transport`, `Expenses → Rent & Utilities`, `Liabilities → Expenses Payable` — unused.

### Why it was done this way (and why the fix must be additive)

A booking's **only** carrier of money is a priced material line
(`BookingItem = material + qty + rate`; `booking.amount` is a readonly
`Σ qty × rate`, `templates/bookings.html:240` + `updatePrice()`), and the
receivable is derived from it
(`void_rebuild.py:1369`: `pending = amount − discount − paid_amount` → `PendingBill`).
So to charge rent *at booking time*, discount it, cancel the unused part and have
it reach the client ledger, rent had to become a priced line. **That is a
rational workaround — the bug is that in this schema a priced line is also a
stock line.**

---

## 3. Decision taken (owner, this session)

> **Keep every existing function exactly as it works today. Remove rent from
> (a) GRN adding and (b) stock summary.**

Restated as the design rule:

> A rent line stays a **bill line** (qty × rate, in the total, discountable,
> booked, consumed, printed, settled) and stops being a **stock line**
> (no material total, no stock gate, no GRN, no stock report).

Nothing else changes. No new tables, no new bill shape, no re-training of staff.

---

## 4. HARD CONSTRAINT — do not stop writing the `Entry` row

Tempting, and fatal. The rent `Entry` OUT row is the **booking pool ledger**, not
just a stock movement. Removing it silently breaks:

| Dependent behaviour | Where |
|---|---|
| booked-rent availability (`booked − delivered + returned`) | `_direct_sales_add_direct_sale.py:205-224` |
| the same pool on the **edit sale** path | `_direct_sales_edit_direct_sale.py:204` |
| booking **cancellation** refund `remaining × rate` | `app/services/booking_cancel_plan.py` |
| "Booked Sale = booked materials only" + booked/chargeable auto-split | `_direct_sales_add_direct_sale.py:257-330` |
| booked-material **return** eligibility (which already carries a real `rent_rate`) | `app/services/drafts.py:216+`, `returns.py:176-186` |
| dispatch quantity guard | `app/blueprints/ops/_dispatch_add_record.py:60-88` |
| bill/PDF line ↔ movement matching (alternate-material label) | `_bills_download_invoice.py:145-167` |

**Therefore:** keep writing `Entry`; remove only the *stock effect*. This is what
makes "everything else identical" actually achievable.

---

## 5. Design

### 5.1 One flag, one helper

1. **`Material.is_stock_tracked`** (Boolean, default `True`) — added the
   established repo way: `app/services/schema.py::_ensure_model_columns` **plus** a
   new `app/migrations/0003_material_stock_tracked.sql`. Never edit an applied
   migration (see `app/migrations/README.md`). This is the same pattern used for
   `Supplier.transferred_to_id` in `GRN_SUPPLIER_SHIFT_PLAN.md`.
2. **`is_stock_material(material_or_name)`** — one function, one truth. During the
   transition it also returns `False` for names matching today's
   `'rent' in name` heuristic, so nothing slips through before the flags are set.
3. **Why per-item and not per-category:** the pseudo-materials are scattered —
   `RENT-CEMENT/STEEL/GENERAL` + `LABOUR` are in category `RENT`, but `RENT STEEL`
   (53 lines, −22,707) is in category `General` and `CHAUGATH-LABOUR` (59 lines) is
   in `CHAUGATH`. A category rule misses two thirds; a name rule dies on the next
   rename (`/materials/rename_label`, `/merge_materials` cascade-rename every
   table).
4. **Why no data conversion is needed:** stock totals are a full recompute
   (`_rebuild_material_totals`). Excluding non-stock materials makes the −84k
   holes simply stop appearing in stock, with **no transaction edited** — and
   flipping the flag back restores them. Fully reversible by design.

### 5.2 Surfaces that must change

**A. Refused by GRN** (owner ask #1)

| Site | Today | Target behaviour |
|---|---|---|
| `app/blueprints/ops/grn.py:322` (picker) + `:196-236` (item loop: `mat.total += qty`, `Entry IN`) | any material accepted | non-stock absent from the picker **and rejected on save** with a plain message: *“RENT-CEMENT is a charge, not stock — no GRN is needed for it.”* |
| `app/blueprints/misc/pending.py:538` (edit picker), `:265`, `:489` (`/edit_grn` stock writes) | same, on edit | identical guard |
| `templates/grn_wizard.html` item autocomplete | lists all materials | lists stock-tracked only |
| `blueprints/import_export/sheets_master_supply.py` + engine (GRN sheet) | would accept a rent row | **row error**, never a silent skip |
| `_grn_has_locked_lots` / lock-refresh paths | n/a | rent lines must never lock a lot (see B) |

**B. Ignored by stock measurement** (owner ask #2)

| Site | What it must stop counting |
|---|---|
| `app/services/void_rebuild.py:1876 _rebuild_material_totals` | the authoritative recompute → skip non-stock, pin their `total` at 0 |
| `blueprints/inventory/stock.py::stock_summary` | `stats` rows, `totals_by_material`, `day_wise`, `grand_received/delivered/net`, material + category picker lists, `_grn_receipt_history` |
| `app/blueprints/core.py:33-45` | dashboard “Current Stock by Brand” |
| `app/blueprints/ledgers/other.py:255-306` | `/material_ledger/<id>` page + its material picker |
| `app/blueprints/sales/_direct_sales_add_direct_sale.py:376-383` | **the only stock gate in the app** → skip non-stock, so rent no longer needs “Allow Negative Stock” |
| incremental writers `void_rebuild.py:269/305/373/1207/1229`, `_dispatch_add_record.py:115`, `_dispatch_edit_entry.py:58-65`, `returns.py:262/414/457`, `misc/pending.py:265/491`, `blueprints/import_export/sheets_master_party.py:160` | guard all with the same helper (GRN/import ones become unreachable anyway) |
| `tools/health/preflight_check.py` check `b1` | “N materials negative → sales blocked” must not count non-stock |
| `app/blueprints/misc/_wipe_system_report.py:49-51` | stock counts in the system report |
| `app/services/sales_core.py:259-292 _expand_chargeable_items_fifo` | skip non-stock → no `GRNAllocation`, no `GRNItem.is_locked`, no frozen `cost_rate_at_sale` on a rent line |
| `app/blueprints/reports/_profit_profit_reports.py:378-388` | treat a non-stock line as **cost 0 and known** so rent stops landing in the “COGS unknown” bucket (margin on rent = rent charged − driver rent, i.e. exactly today's variance block) |

**C. Deliberately untouched — this is what “same function” means**

- Booking add/edit/void; `amount = Σ qty × rate`; `pending = amount − discount − paid`;
  `PendingBill` (`void_rebuild.py:1348`); paid-now-into-account; **cancellation plan**;
  FIFO settlement of client payments (`void_rebuild.py:1519`); bill view + PDF/print.
- Sale: `Cash / Credit / Open Khata / Booking Delivery / Mixed` rules; the
  `RENT CHARGED TO CLIENT` / `DELIVERY PERSON RENT (ACTUAL)` / `RENT DIFFERENCE`
  box (`templates/direct_sales.html:750-764`); `rent_item_revenue`,
  `delivery_rent_cost`, `rent_variance_loss`.
- Delivery: `SaleDeliveryPerson` → `/delivery_rents` → Pay / waive-off →
  `DeliveryPersonPayment` → `AccountTransaction('Driver Payment')` → driver ledger
  FIFO (`app/services/driver_payments.py`, `app/blueprints/ledgers/delivery_person.py`).
- Client financial ledger rows, incl. the informational “Delivery Rent Variance
  (Company Loss)” line (`app/services/ledgers.py:400-430`).
- Profit report rent-variance block (`_profit_profit_reports.py:427-546`).
- `Entry` rows themselves (they remain the booking-pool audit trail).

### 5.3 What the user will notice

| Screen | Before | After |
|---|---|---|
| GRN wizard | any material; pressure to invent a "rent GRN" | rent/charge items not offered, refused if forced |
| Stock summary / dashboard / material ledger | rent rows with huge negative balances | rent rows absent; only real goods |
| Booking / Direct sale form | unchanged | unchanged (same lines, same rent box, same totals) |
| Posting a rent line with global negative stock OFF | blocked when stock short | always posts (no stock concept involved) |
| Profit report | rent cost "unknown", rent margin muddied by COGS lookups | rent revenue kept, cost explicitly 0, margin = charged − driver rent |

---

## 6. Open decisions (owner sign-off required before any code)

| # | Question | Options | Recommendation |
|---|---|---|---|
| 1 | Which items become non-stock? | (a) the 4 `RENT*` only (b) + `LABOUR`, `CHAUGATH-LABOUR`, `MISC`, `BACK FILLING` (c) a reviewed list of the 50 zero-GRN materials | **(b)** — same disease, no side effects. (c) belongs to a separate stock-discipline exercise. |
| 2 | `/daily_transactions` (movement log) — hide rent rows? | hide / keep | **Keep** — it is the booking-pool audit trail void/edit relies on |
| 3 | Client **material** ledger (booked vs dispatched per client) — hide rent? | hide / keep | **Keep** — that page is about what the client is owed, not what is in the store |
| 4 | Turn `allow_global_negative_stock` OFF? | now / later | **Later** — 40+ *real* materials are negative (`ISM 12MM STEEL −113,443`, `12MM STEEL −83,251`, `BRICKS −22,319`); switching off would block genuine sales. Separate GRN-discipline project. |
| 5 | Relabel `RENT-CEMENT` → `Delivery Rent` on bills? (`/materials/rename_label` cascades all tables) | yes / no | **No, not now.** With the explicit flag a rename finally becomes *safe*; do it after phase 1 lands, if at all |

---

## 7. Rollout phases (each independently shippable)

| Phase | Content | Acceptance criteria |
|---|---|---|
| **P0 — schema + helper** | `Material.is_stock_tracked` (default true) via `_ensure_model_columns` + `0003_*.sql`; `is_stock_material()` (flag **or** legacy `'rent' in name`); Materials master: checkbox + list column + filter | new/old DBs boot clean; flag defaults to stock-tracked for all 70 materials; no behavioural change yet |
| **P1 — GRN refusal** | picker filter + server guard on `/grn` add and `/edit_grn`; import row error | `GRNItem` can never reference a non-stock material; clear user-facing message; a rent GRN import fails loudly |
| **P2 — stock exclusion** | `_rebuild_material_totals` + the ~10 stock readers/writers in §5.2B | stock summary, dashboard and material ledger show **no** non-stock row; `RENT-*` totals pinned at 0; `b1` preflight stops flagging them |
| **P3 — sale/booking gate + FIFO** | stock gate skips non-stock; `_expand_chargeable_items_fifo` skips non-stock (no allocation/lock/cost) | a rent-only sale posts with `allow_negative_stock` **OFF**; no `GRNAllocation` or `is_locked` ever touches a rent line; booking consumption & cancel still exact |
| **P4 — P&L hygiene** | rent lines cost-known-0; rent variance block sourced from the flag as well as the name fallback | rent revenue unchanged; "COGS unknown" bucket no longer contains rent lines; `rent_item_revenue/cost/variance` re-verified by `rebuild_direct_sale_effects` |
| **P5 — data hygiene (ops, not code)** | set the flags on the agreed list; void any rent GRN / driver-as-supplier rows created after 2026-10-20; clean residue with **Shift Supplier Ledger / `hard_delete_supplier`**; keep `RENT-*` materials **inactive, never deleted** (edit paths allow historical inactive names via `resolve_transaction_material(allow_inactive_names=…)`); take a `.amsdb` snapshot first | no fake vendor/purchase residue; supplier payables KPI clean |

---

## 8. Test plan (pytest, mirrors existing suites)

New module `tests/test_non_stock_charge_items.py` + additions to
`tests/test_predator_regressions.py`-style guards:

1. Rent-only sale saves with `allow_global_negative_stock=False` and no
   `allow_negative_stock` checkbox → *the core promise*.
2. Booking with `RENT-CEMENT` line: booking balance consumed by a `Booking Delivery`
   sale at dispatch **exactly as today**; cancel plan still refunds `remaining × rate`.
3. `stock_summary`, `/` dashboard, `/material_ledger` contain **no** non-stock material row.
4. `Material.total` for a non-stock material stays `0` after sale, edit, void,
   un-void, return, GRN attempt and full rebuild.
5. `/grn` add **and** `/edit_grn` reject a non-stock material line; the item is not in
   the picker; the Excel import reports a row error.
6. No `GRNAllocation`, no `GRNItem.is_locked`, no `cost_rate_at_sale` is ever written
   for a non-stock line; `DeliveryRent`/`SaleDeliveryPerson` totals unchanged.
7. `rent_item_revenue / delivery_rent_cost / rent_variance_loss` byte-identical to
   today for a fixture set of historical sales (regression on "same function").
8. Driver payment flow untouched: `DeliveryPersonPayment` → one
   `AccountTransaction('Driver Payment')` → driver ledger + account agree (already
   covered by existing tests — must still pass).
9. Flag is **reversible**: set back to stock-tracked → totals recompute to the old
   numbers on the next rebuild.
10. `tools/consistency_report.py` + `tools/health/preflight_check.py` clean on a
    fixture DB containing rent-only bills.

---

## 9. Rollback

- Flip `is_stock_tracked` back to `1` (or drop the migration's effect by defaulting
  the helper to `True`): `_rebuild_material_totals` restores today's numbers on the
  next save, because no row was ever edited.
- Keep the migration additive (`ADD COLUMN`), never destructive
  (`MIGRATIONS_ALLOW_DESTRUCTIVE=0` is the default, per README).
- Snapshot the DB (`.amsdb`) before P5 (ops hygiene), as always.

## 10. Risks / watch-list

| Risk | Mitigation |
|---|---|
| A stock reader is missed → rent still shows somewhere | enumerate readers in P2 with a repo-wide grep for `Entry.type`, `Material.total`, `is_stock_material` coverage test |
| Staff previously "fixed" stock with a manual IN (`/add_record`) for rent | guard `add_record`/`edit_entry` too; then the loophole is closed and their old patch rows stay harmless |
| Reports for pre-change periods look different (rent leaves "COGS unknown") | P4 only; document; keep the rent variance rows |
| Turning the global negative-stock switch off later blocks real sales | explicitly deferred (decision #4); separate GRN-discipline plan |
| Renaming/merging a rent material in future | safe after P0 (flag, not name) — that is a *benefit* |

## 11. Known defects found while studying this (log now, fix separately)

1. **The edit-sale path has no stock validation at all** — `_direct_sales_add_direct_sale.py:376-383` checks stock; `_direct_sales_edit_direct_sale.py` re-prices/re-splits with no equivalent check ⇒ an existing sale can be edited into negative stock.
2. **`rent_item_revenue` double-counts booked rent** (delivery-time rent lines fall back to the *booking* rate while the P&L already recognises that money on the booking line) ⇒ the "Rent Difference" figure is directional, not auditable.
3. **Driver money is outside the system** — 0 `DeliveryPersonPayment` rows vs Rs 1,447,815 of driver rent, and only 2 `Expense` transactions (Rs 1,420) ⇒ cash/bank balances and daily reconciliation are overstated. The in-app path exists; it is simply unused. Process change, zero code.
4. **Rent identity = a substring of a free-text name** in two languages (Python + JS) ⇒ any rename silently switches rent analytics off.
5. **`/grn` auto-creates a `Supplier` from free text** ⇒ junk vendor masters (the snapshot already carries suppliers literally named `1`…`6`).
6. `GRN` cost lookups use substring matching (`_report_cost_for_material`: `needle in name`) ⇒ cross-material cost bleed is possible.

## 12. Phase 2 (the eventual shape — out of scope now, recorded so we don't re-litigate)

The long-term correct model, which this plan deliberately does **not** require yet:

1. **`rent_rate` on the line** — `BookingItem(unit_rate, rent_rate)` and
   `DirectSaleItem(unit_rate, rent_rate)`, mirroring the field the app **already**
   uses for returns (`models/sales.py MaterialReturnItem.rent_rate`;
   `returns.py:196-198` computes `qty × (unit_rate + rent_rate)`, moves only `qty`
   in stock). Then: bill total includes rent automatically → `PendingBill`,
   discount, cancel plan keep working untouched; rent can never drift from the
   material qty (fixes 31,952 vs 32,076); `RENT-*` materials retire; the name
   heuristic dies; `rent_charged = Σ qty × rent_rate` becomes exact.
2. **Rent charged once, at the booking** (the money moment) and **never re-priced at
   dispatch**; the dispatch slice reads the booked rent rate. Fixes the double count.
3. **Trip view**: `BookingAllocation` (what this delivery consumed, incl. its rent
   rate) ↔ `SaleDeliveryPerson` (what this trip owes) → per-trip *charged vs payable
   vs margin*. All rows already exist; nothing computes the pair.
4. **Rate card** (Rs/bag per material family per driver, with manual override) → the
   driver payable is derived, not typed; kills the 1,807 / 484 mismatch classes.
5. **Real P&L homes**: `Service Income` (rent recovered) and `Delivery & Transport`
   / `Expense Payable` (driver) from `blueprints/accounts/classification.py` —
   today zero accounts exist in those classes.
6. **Discount scope** (`material only` / `rent only` / `whole bill`): discounts are
   currently bill-level and pro-rata across every line
   (`discount_share = gross_revenue / sale_gross × adjustment`), so a discount
   silently eats rent margin.
7. A per-trip/fixed rent (and loading/freight) would need **bill-level charge lines**
   instead — a bigger change (bill print, edit modal, drafts, returns, cancel, API,
   import/export; ~40 files already touch rent). Only if the shop truly bills rent
   per trip rather than per bag.
8. `models/rentals.py` (day-rate equipment rental) stays untouched unless the
   business starts renting machinery/formwork by day — a different world, and it must
   not be merged with delivery rent.

## 13. Immediate, zero-code actions available today

1. **Stop creating rent GRNs.** Nothing needs them; the app currently only complains
   because rent is stock-tracked. (Snapshot proof: 0 rent GRN lines exist.)
2. **Never name a supplier after a driver.** The driver's home is
   `DeliveryPerson` → `/delivery_rents` → Pay (FIFO, waive-off, cash-aware, already built).
3. **Start recording driver payments in AMS** — Rs 1.44M currently leaves outside the
   system, so no cash reconciliation can ever match.
4. Decide §6 (1)–(5). On confirmation, the next deliverable is the P0–P5
   implementation plan (file-by-file diff outline, migration order, test code,
   rollback) — still with **no code changes until explicitly ordered**.

---

### Sources read

`models/{catalog,sales,stock,delivery,rentals,cash,parties,core}.py` ·
`app/services/{lookups,sales_core,grn_svc,void_rebuild,driver_payments,ledgers,booking_cancel_plan,drafts,classification,accounting,api,schema}.py` ·
`app/blueprints/{ops/grn.py,ops/delivery.py,ops/_dispatch_*.py,sales/_direct_sales_*.py,sales/bookings.py,sales/returns.py,sales/_bills_*.py,misc/pending.py,misc/materials.py,ledgers/{other,delivery_person,_client_*}.py,reports/_profit_profit_reports.py,masters/*}` ·
`blueprints/inventory/{stock,daily}.py` · `blueprints/accounts/*` ·
`templates/{direct_sales,bookings,grn_wizard,delivery_rents,view_bill,material_ledger,_direct_sale_edit_modal}.html` ·
`tools/health/preflight_check.py` · `GRN_SUPPLIER_SHIFT_PLAN.md` · `README.md` ·
read-only SQL over `LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb` (2026-10-20).
