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
