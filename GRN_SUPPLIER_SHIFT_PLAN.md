# Plan: Graceful GRN Supplier Correction / Data Shifting

## Problem
A GRN (Goods Receipt Note) was recorded against the wrong supplier. By the
time the error is noticed, part or all of that stock has already been sold to
customers, so the GRN lots are locked (`GRNItem.is_locked = True`) and the
existing `hard_delete_supplier` path refuses to touch them. The business still
needs the GRN, its inventory effect and the customer sales to remain intact,
while the supplier attribution, supplier ledger and supplier payments move to
the correct supplier.

## Goal
- Re-attribute one or many GRNs from a wrong supplier to the correct supplier.
- Preserve inventory quantities and material totals (no stock change).
- Preserve customer sales and their FIFO cost links (`GRNAllocation`,
  `DirectSaleItem.grn_item_id`, `cost_rate_at_sale`).
- Move supplier ledger ownership and supplier payments so the target supplier's
  ledger shows the GRN and payments, and the source supplier no longer does.
- Keep accounting consistent (auto GRN supplier payments re-posted to target).
- Work even when GRN lots are locked/consumed by sales.
- Be reversible/auditable and non-destructive (source supplier is deactivated
  and marked `transferred_to_id`, not deleted).

## Approach
1. Add `Supplier.transferred_to_id` (auto-migrated by `_ensure_model_columns`)
   so a transfer can be recorded and later reclaimed, mirroring `Client`.
2. Implement `shift_grn_to_supplier(grn, target_supplier)`:
   - set `GRN.supplier_id` / `GRN.supplier` to target
   - call `_sync_grn_auto_supplier_payment(grn, old_supplier_id=...)` so the old
     auto-payment is voided/reversed and a new one is created for the target
   - update IN `Entry` rows for that GRN's `auto_bill_no` to the target name
   - leave `GRNItem`, `GRNAllocation`, `DirectSale*` untouched
3. Implement `transfer_supplier(source, target, grn_ids=None)`:
   - move every GRN of the source supplier (by id or legacy name match)
   - move legacy IN entries not tied to a GRN
   - move standalone `SupplierPayment` rows to target
   - remove now-voided auto-payments for moved GRNs
   - deactivate + mark source as transferred
   - audit via `audit_log` and `record_accounting_audit`
4. Expose endpoints `/transfer_supplier/<id>` and `/shift_grn_supplier/<id>` with
   `can_manage_suppliers` permission, plus UI option in the supplier modal.
5. Alias names (`transfer_supplier_data`, `move_grn_to_supplier`,
   `reassign_grn_supplier`, `move_supplier_grns`) for compatibility.

## Safety / Invariants
- Material totals unchanged (`_rebuild_material_totals` not needed; quantities
  untouched).
- Customer sales unchanged; only supplier attribution moves.
- Auto-payments are re-posted, not duplicated; old voided rows removed.
- Source supplier deactivated to prevent new entries against the wrong party.
- Period-open checks still apply to auto-payment creation.

## Rollback
- Source supplier can be reactivated manually; a full reclaim can move data
  back using the same primitives if required.

---

# Extension: full supplier-ledger shift (clean move)

## Problem the first pass left open
`transfer_supplier` re-attributed GRNs and *active* payments only. After a
transfer the old supplier still owned voided payments, its opening balance,
legacy `Entry` stock rows, the accounts linked to it and any manual cash-flow
rows — so its ledger was never empty, the payables KPIs still showed it, and
`hard_delete_supplier` could still be blocked.

## What the ledger shift adds
`app/services/supplier_ledger_shift.py`:

| Function | Purpose |
|---|---|
| `preview_supplier_ledger_shift(source, target=None, **options)` | Counts/amounts of everything that will move (used by the preview page + JSON API) |
| `shift_supplier_ledger(source, target, actor=..., **options)` | The move: GRNs → payments → legacy entries → opening balance → accounts → cash flow → suspend source |
| `shift_supplier_payment_to_supplier(payment, target)` | Single standalone payment row move (Accounts → Supplier Payments) |
| `reclaim_supplier_ledger(source)` | Reverses the last shift from the ids stored in `accounting_audit_log` |
| `create_target_supplier(name)` | "Shift to a brand new supplier" flow |

Ordering matters: **payments move first**, then GRNs are re-attributed and
`_sync_grn_auto_supplier_payment` refreshes each GRN's auto-payment in a
savepoint. That way the auto-payment row is *updated in place* instead of being
voided and re-posted, so the cash/bank balance is never double-counted and a
finalised reconciliation period cannot abort the whole shift (it is reported as
a warning instead).

## Options (all default on, all toggleable in the UI)
`move_opening_balance`, `move_legacy_entries`, `move_cash_flow`,
`relink_accounts`, `rename_accounts`, `deactivate_source`.
GRNs and supplier payments always move — they *are* the ledger.

## Routes / UI
* `GET /supplier_ledger_shift/<id>` — preview page (`templates/supplier_ledger_shift.html`)
* `GET /api/supplier_ledger_shift_preview/<id>` — JSON preview
* `POST /shift_supplier_ledger/<id>` — perform (requires `confirm=1`)
* `POST /shift_supplier_payment/<id>` — single payment move
* `POST /reclaim_supplier_ledger/<id>` — reclaim
* Suppliers page delete modal → *Option 1c: Shift Entire Ledger (Clean)*
* Supplier ledger header → *Shift entire ledger* / *Reclaim shifted ledger*
* Supplier ledger rows → per-row shift icon (GRN, or standalone payment)

`shift_grn_supplier` now honours an optional `return_to` form field (same-site
paths only) so the ledger-row modal returns to the ledger page.

## Invariants kept
* Inventory quantities, material totals, `GRNItem` lots/locks, `GRNAllocation`
  and `DirectSaleItem` FIFO links are untouched.
* Everything happens in one transaction; per-GRN auto-payment refreshes use
  savepoints and only downgrade to warnings.
* `accounting_audit_log` gets a `LedgerShift` record containing the moved row
  ids, the opening-balance delta, the account ids/name restore map and the
  cash-flow ids — enough for a faithful reclaim.
