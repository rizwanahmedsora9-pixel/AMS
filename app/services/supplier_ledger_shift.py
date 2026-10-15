"""Move a supplier's **entire ledger footprint** to another supplier.

The Accounts section is a first-class participant in this move rather than an
afterthought: the rows that make up a supplier ledger are the same rows the
Accounts module reads and writes —

``Supplier.opening_balance`` -> ``GRN`` -> ``SupplierPayment`` ->
``AccountTransaction`` (cash/bank outflow) -> ``CashFlowEntry`` -> linked
``Account``

so shifting them here keeps *Supplier Ledger*, *Accounts -> Supplier Payments*,
the payables KPIs, the cash-flow page and the linked-account widgets in step
without any recomputation pass.

Why the plain ``transfer_supplier`` helper is not enough
--------------------------------------------------------
``transfer_supplier`` (see :mod:`app.services.void_rebuild`) was written for the
narrow "this GRN went to the wrong supplier" case: it re-attributes GRNs and
active payments.  It deliberately leaves behind

* voided / already-reversed supplier payments,
* the supplier's opening balance,
* legacy ``Entry`` stock rows that carry the old supplier name,
* accounts linked to the supplier, and
* manual cash-flow rows recorded against the supplier,

so the source ledger never actually becomes empty and the supplier cannot be
deleted cleanly.  :func:`shift_supplier_ledger` closes all of those gaps and is
therefore the "make the old supplier ledger clean" operation.

Invariants
----------
* Inventory quantities, material totals, GRN line items, FIFO allocations and
  customer sales are untouched (only attribution moves).
* Every mutation happens inside the caller's transaction: the whole shift
  either commits or rolls back.
* Per-GRN auto-payment refreshes run in savepoints, so one closed-period GRN
  cannot abort the rest of the shift (it is reported as a warning instead).
* The move is recorded in ``accounting_audit_log`` with enough detail
  (moved row ids, opening balance, account ids) for
  :func:`reclaim_supplier_ledger` to reverse it.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import and_, func, or_

from models import (
    Account,
    AccountTransaction,
    AccountingAuditLog,
    CashFlowEntry,
    CashFlowParty,
    Entry,
    GRN,
    Supplier,
    SupplierPayment,
    db,
)
from utils.audit import audit_log

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# options + small helpers
# --------------------------------------------------------------------------
DEFAULT_OPTIONS = {
    # Everything that is not in here is ALWAYS moved: GRNs and supplier
    # payments are the ledger itself, so they never stay behind.
    'move_opening_balance': True,
    'move_legacy_entries': True,
    'move_cash_flow': True,
    'relink_accounts': True,
    'rename_accounts': True,
    'deactivate_source': True,
}


def _norm(value) -> str:
    return str(value or '').strip().lower()


def _money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _resolve_supplier(obj_or_id):
    if isinstance(obj_or_id, Supplier):
        return obj_or_id
    if obj_or_id in (None, ''):
        return None
    try:
        return db.session.get(Supplier, int(obj_or_id))
    except (TypeError, ValueError):
        return None


def resolve_options(**overrides):
    """Build a validated option dict (unknown keys are ignored)."""
    opts = dict(DEFAULT_OPTIONS)
    for key, value in (overrides or {}).items():
        if key in opts and value is not None:
            opts[key] = bool(value)
    return opts


def _source_grn_query(source: Supplier):
    """Every GRN the supplier ledger projection would show for ``source``.

    The projection matches on ``supplier_id`` *or* on the supplier name (legacy
    rows saved before ``supplier_id`` existed), so the shift must use the same
    rule or the ledger will not become empty.
    """
    name = _norm(source.name)
    return GRN.query.filter(
        or_(
            GRN.supplier_id == source.id,
            # Legacy rows saved before supplier_id existed (or saved with a
            # stale id) are matched by name, exactly like the ledger does.
            func.lower(func.trim(func.coalesce(GRN.supplier, ''))) == name,
        )
    )


def _source_payment_query(source: Supplier):
    return SupplierPayment.query.filter(SupplierPayment.supplier_id == source.id)


def _grn_ref(grn) -> str:
    return (
        getattr(grn, 'manual_bill_no', None)
        or getattr(grn, 'auto_bill_no', None)
        or f"GRN #{getattr(grn, 'id', '')}"
    )


def _supplier_balances(*suppliers):
    """Closing payable per supplier id, using the canonical projection."""
    wanted = [s for s in suppliers if s is not None]
    if not wanted:
        return {}
    try:
        from app.services.financial_ledgers import build_supplier_payable_summaries
        return build_supplier_payable_summaries(wanted)
    except Exception:  # pragma: no cover - projection must never break a shift
        logger.exception('Supplier payable projection failed while shifting ledger')
        return {s.id: _money(s.opening_balance) for s in wanted}


def _cash_flow_party_query(source: Supplier):
    name = _norm(source.name)
    return CashFlowParty.query.filter(
        func.lower(func.trim(func.coalesce(CashFlowParty.name, ''))) == name,
    )


def _cash_flow_entry_query(source: Supplier, party_ids=None):
    name = _norm(source.name)
    clauses = [
        and_(
            func.lower(func.coalesce(CashFlowEntry.party_type, '')).like('supplier%'),
            func.lower(func.trim(func.coalesce(CashFlowEntry.party_name, ''))) == name,
        )
    ]
    if party_ids:
        clauses.append(CashFlowEntry.party_id.in_(list(party_ids)))
    return CashFlowEntry.query.filter(or_(*clauses))


def _legacy_entry_query(source: Supplier):
    name = _norm(source.name)
    return Entry.query.filter(
        func.lower(func.trim(func.coalesce(Entry.client, ''))) == name,
    )


def _count(query) -> int:
    try:
        return int(query.count() or 0)
    except Exception:
        return 0


def _total(query, column) -> float:
    try:
        value = query.with_entities(func.coalesce(func.sum(column), 0)).scalar()
        return _money(value)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------
def preview_supplier_ledger_shift(source, target=None, **options):
    """Summarise exactly what :func:`shift_supplier_ledger` will move."""
    source = _resolve_supplier(source)
    if not source:
        raise ValueError('Supplier not found.')
    target = _resolve_supplier(target) if target is not None else None
    opts = resolve_options(**options)

    grn_q = _source_grn_query(source)
    grns = grn_q.all()
    payment_q = _source_payment_query(source)
    payments = payment_q.all()

    locked = []
    for grn in grns:
        try:
            from app.services.sales_core import _grn_has_locked_lots
            if _grn_has_locked_lots(grn):
                locked.append(_grn_ref(grn))
        except Exception:
            pass

    parties = _cash_flow_party_query(source).all()
    party_ids = [p.id for p in parties]
    cf_q = _cash_flow_entry_query(source, party_ids) if opts['move_cash_flow'] else None

    accounts = Account.query.filter(Account.linked_supplier_id == source.id).all()
    legacy_q = _legacy_entry_query(source)

    balances = _supplier_balances(source, target)
    source_rows_before = len(_ledger_rows(source))

    target_account_conflict = None
    if target is not None:
        target_account_conflict = (
            Account.query.filter(Account.linked_supplier_id == target.id).count()
        )

    payment_account_ids = sorted({
        p.payment_account_id for p in payments if getattr(p, 'payment_account_id', None)
    })
    account_tx_count = 0
    payment_ids = [p.id for p in payments]
    if payment_ids:
        markers = [f'%[SRC:SupplierPayment:{pid}]%' for pid in payment_ids]
        account_tx_count = _count(AccountTransaction.query.filter(
            or_(
                and_(
                    AccountTransaction.source_type == 'SupplierPayment',
                    AccountTransaction.source_id.in_(payment_ids),
                ),
                *[AccountTransaction.note.ilike(marker) for marker in markers],
            )
        ))

    return {
        'source': {
            'id': source.id,
            'name': source.name,
            'is_active': bool(source.is_active),
            'opening_balance': _money(source.opening_balance),
            'ledger_rows': source_rows_before,
            'closing_balance': _money(balances.get(source.id, source.opening_balance)),
        },
        'target': (
            {
                'id': target.id,
                'name': target.name,
                'is_active': bool(target.is_active),
                'opening_balance': _money(target.opening_balance),
                'closing_balance': _money(balances.get(target.id, target.opening_balance)),
            }
            if target is not None else None
        ),
        'grns': len(grns),
        'grn_total': _money(sum(_grn_total(grn) for grn in grns)),
        'locked_grns': locked,
        'payments': len(payments),
        # Active money only (that is what the supplier ledger totals show).
        'payments_total': _money(sum(
            _money(p.amount) for p in payments if not getattr(p, 'is_void', False)
        )),
        'active_payments': len([p for p in payments if not getattr(p, 'is_void', False)]),
        'voided_payments': len([p for p in payments if getattr(p, 'is_void', False)]),
        'voided_payments_total': _money(sum(
            _money(p.amount) for p in payments if getattr(p, 'is_void', False)
        )),
        'auto_payments': len([
            p for p in payments
            if _norm(getattr(p, 'source_type', '')) == 'grn' or '[auto_grn_pay:' in _norm(p.note)
        ]),
        'payment_account_ids': payment_account_ids,
        'account_transactions': account_tx_count,
        'opening_balance_movable': _money(source.opening_balance) if opts['move_opening_balance'] else 0.0,
        'legacy_entries': _count(legacy_q) if opts['move_legacy_entries'] else 0,
        'linked_accounts': [
            {'id': a.id, 'name': a.name, 'category': a.category, 'channel': a.channel}
            for a in accounts
        ],
        'target_linked_accounts': target_account_conflict or 0,
        'cash_flow_parties': [
            {'id': p.id, 'name': p.name, 'party_type': p.party_type} for p in parties
        ],
        'cash_flow_entries': _count(cf_q) if cf_q is not None else 0,
        'options': opts,
        'projected_source_rows': 0,
    }


def _grn_total(grn) -> float:
    try:
        from app.services.grn_svc import calculate_grn_total
        return _money(calculate_grn_total(grn))
    except Exception:
        total = 0.0
        for item in (getattr(grn, 'items', None) or []):
            if getattr(item, 'is_void', False):
                continue
            total += _money(getattr(item, 'qty', 0)) * _money(getattr(item, 'price_at_time', 0))
        total += _money(getattr(grn, 'loading_cost', 0))
        total += _money(getattr(grn, 'freight_cost', 0))
        total += _money(getattr(grn, 'other_expense', 0))
        total += _money(getattr(grn, 'tax_amount', 0))
        total -= _money(getattr(grn, 'discount', 0))
        total += _money(getattr(grn, 'adjustment_amount', 0))
        return _money(total)


def _ledger_rows(supplier) -> list:
    try:
        from app.services.financial_ledgers import build_supplier_financial_ledger
        return list(build_supplier_financial_ledger(supplier).get('rows') or [])
    except Exception:
        logger.exception('Supplier ledger projection failed')
        return []


# --------------------------------------------------------------------------
# the shift itself
# --------------------------------------------------------------------------
def create_target_supplier(name, *, phone='', address='', actor=None):
    """Find-or-create the receiving supplier (used by the "shift to new" flow)."""
    name = (name or '').strip()
    if not name:
        raise ValueError('Enter a name for the new supplier.')
    existing = Supplier.query.filter(
        func.lower(func.trim(Supplier.name)) == name.lower()
    ).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
        return existing, False
    supplier = Supplier(
        name=name,
        phone=(phone or '').strip(),
        address=(address or '').strip(),
        opening_balance=0.0,
        is_active=True,
    )
    db.session.add(supplier)
    db.session.flush()
    try:
        audit_log(actor, 'supplier.create', f"Created supplier '{name}' (#{supplier.id}) as a ledger-shift target.")
    except Exception:
        pass
    return supplier, True


def shift_supplier_ledger(source, target, *, actor=None, **options):
    """Move every ledger row of ``source`` to ``target``.

    ``target`` may be a :class:`~models.Supplier`, an id, or a name; a name that
    does not exist yet is created so the whole ledger can be pushed onto a brand
    new supplier.

    Returns a result dict that is also stored in the accounting audit trail so
    the move can be reclaimed later.
    """
    source = _resolve_supplier(source)
    if not source:
        raise ValueError('Source supplier not found.')

    target_is_new = False
    if isinstance(target, str) or (target is not None and not isinstance(target, (Supplier, int))):
        target, target_is_new = create_target_supplier(target, actor=actor)
    else:
        target = _resolve_supplier(target)
    if not target:
        raise ValueError('Target supplier not found. Pick one or type a new supplier name.')
    if target.id == source.id:
        raise ValueError('Source and target supplier are the same.')
    if not target.is_active:
        raise ValueError(f"Supplier '{target.name}' is suspended. Activate it or shift to another supplier.")

    opts = resolve_options(**options)
    warnings: list[str] = []

    balances_before = _supplier_balances(source, target)
    source_closing_before = _money(balances_before.get(source.id, source.opening_balance))
    target_closing_before = _money(balances_before.get(target.id, target.opening_balance))
    source_rows_before = len(_ledger_rows(source))

    grns = _source_grn_query(source).all()
    moved_grn_ids: list[int] = []
    locked_grns: list[str] = []

    # ---- 1. supplier payments move first, so the GRN auto-payment refresh
    #         finds its own (moved) row and updates it in place instead of
    #         voiding it and posting a second cash movement.
    payments = _source_payment_query(source).all()
    moved_payment_ids: list[int] = []
    moved_payment_total = 0.0
    for payment in payments:
        payment.supplier_id = target.id
        moved_payment_ids.append(payment.id)
        if not payment.is_void:
            moved_payment_total += _money(payment.amount)
    db.session.flush()

    # ---- 2. GRNs: attribution only. Inventory, lots, FIFO links and customer
    #         sales are deliberately untouched.
    grn_auto_refreshed = 0
    for grn in grns:
        try:
            from app.services.sales_core import _grn_has_locked_lots
            if _grn_has_locked_lots(grn):
                locked_grns.append(_grn_ref(grn))
        except Exception:
            pass
        grn.supplier_id = target.id
        grn.supplier = target.name
        moved_grn_ids.append(grn.id)
    db.session.flush()

    for grn in grns:
        try:
            with db.session.begin_nested():
                from app.services.grn_svc import _sync_grn_auto_supplier_payment
                _sync_grn_auto_supplier_payment(grn)
            grn_auto_refreshed += 1
        except ValueError as exc:
            # A closed reconciliation period blocks re-posting a legacy paid
            # amount.  The read-side ledger still projects that GRN's paid
            # amount, so the target ledger stays correct; surface it instead of
            # failing the whole shift.
            warnings.append(f"GRN {_grn_ref(grn)}: {exc}")
        except Exception:
            logger.exception('GRN auto-payment refresh failed during ledger shift')
            warnings.append(f"GRN {_grn_ref(grn)}: auto-payment could not be refreshed.")

    # Flag (never silently "fix") duplicate auto-payments for one GRN: they
    # would double-count on the receiving ledger, so the operator must see it.
    from collections import defaultdict
    auto_by_grn: dict = defaultdict(list)
    for payment in payments:
        if getattr(payment, 'is_void', False):
            continue
        if _norm(getattr(payment, 'source_type', '')) == 'grn' and getattr(payment, 'source_id', None):
            auto_by_grn[payment.source_id].append(payment)
    duplicate_auto_payments = {
        grn_id: len(rows) for grn_id, rows in auto_by_grn.items() if len(rows) > 1
    }
    for grn_id, count in duplicate_auto_payments.items():
        warnings.append(
            f"GRN #{grn_id} has {count} active auto-payments — review them on the receiving supplier."
        )

    # ---- 3. Payments whose accounting text still names the old supplier get
    #         re-synced so Accounts (transactions, account ledger, cash flow)
    #         shows the new supplier name.
    accounting_resynced = 0
    for payment in payments:
        try:
            with db.session.begin_nested():
                from app.services.accounting import _sync_supplier_payment_accounting
                _sync_supplier_payment_accounting(payment)
            accounting_resynced += 1
        except Exception:
            logger.exception('Supplier payment accounting re-sync failed during ledger shift')
            warnings.append(f"Payment PAY-{payment.id}: accounting text could not be refreshed.")

    # ---- 4. Legacy stock ledger rows (Entry.type IN) named after the supplier.
    entry_bills = sorted({g.auto_bill_no for g in grns if g.auto_bill_no})
    legacy_entry_ids: list[int] = []
    if opts['move_legacy_entries']:
        legacy_rows = {}
        for row in _legacy_entry_query(source).all():
            legacy_rows[row.id] = row
        # IN rows linked to a moved GRN by bill number or by source link.
        if entry_bills:
            for row in Entry.query.filter(
                Entry.type == 'IN', Entry.auto_bill_no.in_(entry_bills)
            ).all():
                legacy_rows[row.id] = row
        if moved_grn_ids:
            for row in Entry.query.filter(
                Entry.type == 'IN',
                Entry.source_module == 'GRN',
                Entry.source_id.in_(moved_grn_ids),
            ).all():
                legacy_rows[row.id] = row
        legacy_entry_ids = sorted(legacy_rows)
        for row in legacy_rows.values():
            row.client = target.name
        db.session.flush()

    # ---- 5. Opening balance (adds onto the target, zeroes the source).
    opening_moved = 0.0
    opening_dates = {'source': None, 'target': None}
    if opts['move_opening_balance']:
        opening_moved = _money(source.opening_balance)
        if opening_moved != 0:
            opening_dates['source'] = source.opening_balance_date.isoformat() if source.opening_balance_date else None
            opening_dates['target'] = target.opening_balance_date.isoformat() if target.opening_balance_date else None
            target.opening_balance = _money(target.opening_balance) + opening_moved
            if not target.opening_balance_date and source.opening_balance_date:
                target.opening_balance_date = source.opening_balance_date
            source.opening_balance = 0.0
            db.session.flush()

    # ---- 6. Accounts section: the account linked to the supplier follows it.
    relinked_accounts: list[int] = []
    renamed_accounts: dict[str, str] = {}
    if opts['relink_accounts']:
        linked = Account.query.filter(Account.linked_supplier_id == source.id).all()
        for account in linked:
            relinked_accounts.append(account.id)
            account.linked_supplier_id = target.id
            account.linked_party_name = target.name
            if opts['rename_accounts'] and _norm(source.name) and _norm(source.name) in _norm(account.name):
                renamed_accounts[str(account.id)] = account.name
                account.name = _swap_name(account.name, source.name, target.name)
        db.session.flush()

    # ---- 7. Cash flow: manual rows recorded against the supplier party.
    cash_flow_entry_ids: list[int] = []
    cash_flow_party_id = None
    cash_flow_party_renamed = False
    if opts['move_cash_flow']:
        parties = _cash_flow_party_query(source).all()
        party_ids = [p.id for p in parties]
        entries = _cash_flow_entry_query(source, party_ids).all()
        if entries or parties:
            entry_types = {
                _norm(getattr(e, 'party_type', '')) for e in entries if getattr(e, 'party_type', None)
            }
            entry_types.discard('')
            ptype = next(iter(entry_types)) if len(entry_types) == 1 else 'supplier'
            target_party = CashFlowParty.query.filter(
                func.lower(func.trim(func.coalesce(CashFlowParty.name, ''))) == _norm(target.name),
                func.lower(func.coalesce(CashFlowParty.party_type, '')) == ptype,
            ).first()
            if target_party is None and parties:
                # Reuse the source party row: keeps the FK identity of every
                # historical cash-flow row and leaves no orphan behind.
                target_party = parties[0]
                target_party.name = target.name
                target_party.party_type = ptype
                target_party.updated_at = _now()
                cash_flow_party_renamed = True
            elif target_party is None and entries:
                target_party = CashFlowParty(
                    name=target.name, party_type=ptype,
                    is_active=True, created_at=_now(), updated_at=_now(),
                )
                db.session.add(target_party)
                db.session.flush()
            cash_flow_party_id = getattr(target_party, 'id', None)
            for entry in entries:
                before = _cash_flow_snapshot(entry)
                entry.party_id = cash_flow_party_id
                entry.party_name = target.name
                entry.party_type = ptype
                entry.description = _swap_name(entry.description or '', source.name, target.name) or entry.description
                entry.updated_by = _actor_name(actor)
                cash_flow_entry_ids.append(entry.id)
                try:
                    with db.session.begin_nested():
                        from app.services.cash_flow_svc import _cf_write_audit
                        _cf_write_audit(
                            entry, 'Edited', before=before, after=_cash_flow_snapshot(entry),
                            reason=f"Supplier ledger shifted from '{source.name}' to '{target.name}'.",
                            actor=actor,
                        )
                except Exception:
                    logger.exception('Cash-flow audit write failed during ledger shift')
            # Park a leftover source-named party row so it cannot capture new rows.
            for party in parties:
                if target_party and party.id == target_party.id:
                    continue
                if not _cash_flow_party_used(party.id):
                    party.is_active = False
                    party.updated_at = _now()
            db.session.flush()

    # ---- 8. Suspend + mark the emptied supplier as transferred.
    if opts['deactivate_source']:
        source.is_active = False
        try:
            source.transferred_to_id = target.id
        except Exception:
            pass
        db.session.flush()

    balances_after = _supplier_balances(source, target)
    source_rows_after = len(_ledger_rows(source))

    payload = {
        'kind': 'supplier_ledger_shift',
        'source_id': source.id,
        'source_name': source.name,
        'target_id': target.id,
        'target_name': target.name,
        'target_was_new': bool(target_is_new),
        'moved_grn_ids': moved_grn_ids,
        'moved_payment_ids': moved_payment_ids,
        'legacy_entry_ids': legacy_entry_ids,
        'relinked_account_ids': relinked_accounts,
        'renamed_accounts': renamed_accounts,
        'cash_flow_entry_ids': cash_flow_entry_ids,
        'cash_flow_party_id': cash_flow_party_id,
        'cash_flow_party_renamed': cash_flow_party_renamed,
        'opening_balance_moved': opening_moved,
        'opening_balance_dates': opening_dates,
        'options': opts,
        'source_rows_before': source_rows_before,
        'source_rows_after': source_rows_after,
        'source_closing_before': source_closing_before,
        'target_closing_before': target_closing_before,
    }

    result = {
        **payload,
        'source_supplier_id': source.id,
        'source_supplier_name': source.name,
        'target_supplier_id': target.id,
        'target_supplier_name': target.name,
        'grns_moved': len(moved_grn_ids),
        'locked_grns': locked_grns,
        'payments_moved': len(moved_payment_ids),
        'payments_total': moved_payment_total,
        'payments_resynced': accounting_resynced,
        'auto_payments_refreshed': grn_auto_refreshed,
        'duplicate_auto_payments': duplicate_auto_payments,
        'legacy_entries_moved': len(legacy_entry_ids),
        'accounts_relinked': relinked_accounts,
        'accounts_renamed': renamed_accounts,
        'cash_flow_entries_moved': len(cash_flow_entry_ids),
        'cash_flow_party_renamed': cash_flow_party_renamed,
        'opening_balance_moved': opening_moved,
        'source_rows_before': source_rows_before,
        'source_rows_after': source_rows_after,
        'source_closing_before': source_closing_before,
        'target_closing_after': _money(balances_after.get(target.id, 0)),
        'source_closing_after': _money(balances_after.get(source.id, 0)),
        'warnings': warnings,
    }

    act = actor if actor is not None else _current_user()
    try:
        audit_log(
            act, 'supplier.ledger_shift',
            f"Shifted the full ledger of '{source.name}' (#{source.id}) to '{target.name}' (#{target.id}): "
            f"{result['grns_moved']} GRN(s), {result['payments_moved']} payment(s), "
            f"opening balance {opening_moved:,.2f}, {len(relinked_accounts)} linked account(s), "
            f"{len(cash_flow_entry_ids)} cash-flow row(s).",
        )
    except Exception:
        logger.exception('Audit log write failed during ledger shift')
    try:
        from utils.accounting_audit import record_accounting_audit
        record_accounting_audit(
            actor,
            action='LedgerShift',
            entity_type='Supplier',
            entity_id=source.id,
            before={
                'id': source.id, 'name': source.name,
                'ledger_rows': source_rows_before, 'closing_balance': source_closing_before,
            },
            after={
                'id': target.id, 'name': target.name,
                'ledger_rows_after_source': source_rows_after,
                'target_closing_after': result['target_closing_after'],
                **payload,
            },
            amount_before=source_closing_before,
            amount_after=result['target_closing_after'],
            party_before_id=source.id,
            party_after_id=target.id,
            reason=(
                f"Full supplier ledger shifted from '{source.name}' to '{target.name}'; "
                "inventory lots, FIFO links and customer sales preserved."
            ),
            module='accounts',
        )
    except Exception:
        logger.exception('Accounting audit write failed during ledger shift')

    logger.info(
        'supplier_ledger_shift source=%s target=%s grns=%s payments=%s accounts=%s warnings=%s',
        source.id, target.id, result['grns_moved'], result['payments_moved'],
        len(relinked_accounts), len(warnings),
    )
    return result


# --------------------------------------------------------------------------
# single-row moves
# --------------------------------------------------------------------------
def shift_supplier_payment_to_supplier(payment, target, *, actor=None):
    """Re-point one supplier payment (Accounts -> Supplier Payments row).

    GRN-generated auto-payments are refused: they are owned by their GRN and
    would be pulled back on the next GRN save.  Shift the GRN instead (the GRN
    supplier shift moves the auto-payment with it) or use the full ledger shift.
    """
    if payment is None:
        raise ValueError('Supplier payment not found.')
    target = _resolve_supplier(target)
    if not target:
        raise ValueError('Target supplier not found.')
    if not target.is_active:
        raise ValueError(f"Supplier '{target.name}' is suspended. Activate it first.")

    old_supplier = db.session.get(Supplier, payment.supplier_id) if payment.supplier_id else None
    old_name = old_supplier.name if old_supplier else ''
    if old_supplier and old_supplier.id == target.id:
        return {
            'payment_id': payment.id, 'from_supplier': old_name,
            'to_supplier': target.name, 'moved': False,
        }

    source_type = _norm(getattr(payment, 'source_type', ''))
    if source_type == 'grn' or '[auto_grn_pay:' in _norm(payment.note):
        grn_id = getattr(payment, 'source_id', None)
        raise ValueError(
            f"PAY-{payment.id} was generated by GRN #{grn_id}. "
            "Shift that GRN to the other supplier instead — its payment follows automatically."
        )

    old = {
        'id': payment.id, 'supplier_id': payment.supplier_id, 'supplier_name': old_name,
        'amount': _money(payment.amount), 'is_void': bool(payment.is_void),
    }
    payment.supplier_id = target.id
    db.session.flush()
    try:
        from app.services.accounting import _sync_supplier_payment_accounting
        _sync_supplier_payment_accounting(payment)
    except Exception:
        logger.exception('Accounting re-sync failed while moving a supplier payment')
    db.session.flush()

    try:
        audit_log(
            actor, 'supplier.payment_shift',
            f"Shifted supplier payment PAY-{payment.id} ({_money(payment.amount):,.2f}) "
            f"from '{old_name}' to '{target.name}'.",
        )
    except Exception:
        pass
    try:
        from utils.accounting_audit import record_accounting_audit
        record_accounting_audit(
            actor, action='ShiftSupplier', entity_type='SupplierPayment', entity_id=payment.id,
            before=old,
            after={'id': payment.id, 'supplier_id': target.id, 'supplier_name': target.name},
            amount_before=old['amount'], amount_after=_money(payment.amount),
            account_before_id=getattr(payment, 'payment_account_id', None),
            account_after_id=getattr(payment, 'payment_account_id', None),
            party_before_id=old['supplier_id'], party_after_id=target.id,
            reason=f"Supplier payment moved from '{old_name}' to '{target.name}' via the supplier ledger.",
            module='accounts',
        )
    except Exception:
        logger.exception('Accounting audit write failed while moving a supplier payment')

    return {
        'payment_id': payment.id, 'from_supplier': old_name,
        'to_supplier': target.name, 'moved': True,
    }


# --------------------------------------------------------------------------
# reclaim
# --------------------------------------------------------------------------
def _latest_shift_payload(source: Supplier):
    row = (
        AccountingAuditLog.query
        .filter(
            AccountingAuditLog.entity_type == 'Supplier',
            AccountingAuditLog.entity_id == source.id,
            AccountingAuditLog.action.in_(['LedgerShift', 'Transfer']),
        )
        .order_by(AccountingAuditLog.created_at.desc(), AccountingAuditLog.id.desc())
        .first()
    )
    if not row or not row.after_json:
        return None
    try:
        payload = json.loads(row.after_json)
    except Exception:
        return None
    if payload.get('kind') != 'supplier_ledger_shift':
        # A plain GRN transfer cannot be reclaimed automatically.
        return None
    return payload


def reclaim_supplier_ledger(source, *, actor=None):
    """Undo the last :func:`shift_supplier_ledger` performed on ``source``."""
    source = _resolve_supplier(source)
    if not source:
        raise ValueError('Supplier not found.')
    payload = _latest_shift_payload(source)
    if not payload:
        raise ValueError(
            'No reclaimable ledger shift was found for this supplier. '
            'Use the GRN supplier shift on the individual GRNs instead.'
        )
    target = _resolve_supplier(payload.get('target_id'))
    if not target:
        raise ValueError('The supplier that received this ledger no longer exists.')

    moved_grn_ids = [int(x) for x in payload.get('moved_grn_ids') or []]
    moved_payment_ids = [int(x) for x in payload.get('moved_payment_ids') or []]

    # 1. payments back first (mirrors the shift order), then their GRNs.
    payment_rows = (
        SupplierPayment.query.filter(SupplierPayment.id.in_(moved_payment_ids)).all()
        if moved_payment_ids else []
    )
    payments_reclaimed = 0
    for payment in payment_rows:
        if payment.supplier_id != target.id:
            continue
        payment.supplier_id = source.id
        payments_reclaimed += 1
    db.session.flush()

    grn_rows = GRN.query.filter(GRN.id.in_(moved_grn_ids)).all() if moved_grn_ids else []
    grns_reclaimed = 0
    for grn in grn_rows:
        if grn.supplier_id != target.id:
            continue
        grn.supplier_id = source.id
        grn.supplier = source.name
        grns_reclaimed += 1
    db.session.flush()

    for grn in grn_rows:
        try:
            with db.session.begin_nested():
                from app.services.grn_svc import _sync_grn_auto_supplier_payment
                _sync_grn_auto_supplier_payment(grn)
        except Exception:
            logger.exception('Auto-payment refresh failed while reclaiming a supplier ledger')
    for payment in payment_rows:
        try:
            with db.session.begin_nested():
                from app.services.accounting import _sync_supplier_payment_accounting
                _sync_supplier_payment_accounting(payment)
        except Exception:
            logger.exception('Payment accounting refresh failed while reclaiming a supplier ledger')

    # 2. opening balance back to the source.
    opening = _money(payload.get('opening_balance_moved'))
    if opening:
        target.opening_balance = _money(target.opening_balance) - opening
        source.opening_balance = _money(source.opening_balance) + opening
        dates = payload.get('opening_balance_dates') or {}
        try:
            from datetime import datetime
            if dates.get('source'):
                source.opening_balance_date = datetime.fromisoformat(dates['source'])
            if dates.get('target'):
                target.opening_balance_date = datetime.fromisoformat(dates['target'])
        except Exception:
            logger.exception('Opening balance date restore failed')
        db.session.flush()

    # 3. accounts back to the source and their original names restored.
    relinked = []
    for account in Account.query.filter(Account.linked_supplier_id == target.id).all():
        if account.id not in set(payload.get('relinked_account_ids') or []):
            continue
        account.linked_supplier_id = source.id
        account.linked_party_name = source.name
        relinked.append(account.id)
    for account in Account.query.filter(Account.id.in_([int(k) for k in (payload.get('renamed_accounts') or {})])).all():
        account.name = (payload.get('renamed_accounts') or {}).get(str(account.id), account.name)
        if account.linked_supplier_id == target.id and account.id in set(payload.get('relinked_account_ids') or []):
            account.linked_supplier_id = source.id
            account.linked_party_name = source.name
    db.session.flush()

    # 4. legacy stock rows back to the source name.
    entry_ids = [int(x) for x in payload.get('legacy_entry_ids') or []]
    entries_reclaimed = 0
    if entry_ids:
        for row in Entry.query.filter(Entry.id.in_(entry_ids)).all():
            row.client = source.name
            entries_reclaimed += 1
        db.session.flush()

    # 5. cash flow rows + party back.
    cf_ids = [int(x) for x in payload.get('cash_flow_entry_ids') or []]
    cf_reclaimed = 0
    party_id = payload.get('cash_flow_party_id')
    party = db.session.get(CashFlowParty, int(party_id)) if party_id else None
    if party and payload.get('cash_flow_party_renamed'):
        party.name = source.name
        party.is_active = True
        party.updated_at = _now()
    for entry in (CashFlowEntry.query.filter(CashFlowEntry.id.in_(cf_ids)).all() if cf_ids else []):
        before = _cash_flow_snapshot(entry)
        entry.party_id = party.id if party else entry.party_id
        entry.party_name = source.name
        entry.party_type = (party.party_type if party else entry.party_type) or 'supplier'
        entry.description = _swap_name(entry.description or '', target.name, source.name) or entry.description
        entry.updated_by = _actor_name(actor)
        cf_reclaimed += 1
        try:
            with db.session.begin_nested():
                from app.services.cash_flow_svc import _cf_write_audit
                _cf_write_audit(
                    entry, 'Edited', before=before, after=_cash_flow_snapshot(entry),
                    reason=f"Supplier ledger reclaimed from '{target.name}'.", actor=actor,
                )
        except Exception:
            logger.exception('Cash-flow audit write failed during reclaim')
    db.session.flush()

    # 6. reactivate the source.
    source.is_active = True
    try:
        source.transferred_to_id = None
    except Exception:
        pass
    db.session.flush()

    result = {
        'source_supplier_id': source.id,
        'source_supplier_name': source.name,
        'target_supplier_id': target.id,
        'target_supplier_name': target.name,
        'grns_reclaimed': grns_reclaimed,
        'payments_reclaimed': payments_reclaimed,
        'opening_balance_reclaimed': opening,
        'accounts_relinked': relinked,
        'entries_reclaimed': entries_reclaimed,
        'cash_flow_entries_reclaimed': cf_reclaimed,
    }
    try:
        audit_log(
            actor, 'supplier.ledger_reclaim',
            f"Reclaimed the ledger of '{source.name}' (#{source.id}) from '{target.name}' (#{target.id}): "
            f"{grns_reclaimed} GRN(s), {payments_reclaimed} payment(s).",
        )
    except Exception:
        pass
    try:
        from utils.accounting_audit import record_accounting_audit
        record_accounting_audit(
            actor, action='LedgerReclaim', entity_type='Supplier', entity_id=source.id,
            before={'id': target.id, 'name': target.name},
            after={'id': source.id, 'name': source.name, **result},
            party_before_id=target.id, party_after_id=source.id,
            reason=f"Ledger reclaimed from '{target.name}'.",
            module='accounts',
        )
    except Exception:
        logger.exception('Accounting audit write failed during reclaim')
    return result


# --------------------------------------------------------------------------
# tiny shared helpers
# --------------------------------------------------------------------------
def _swap_name(text, old_name, new_name) -> str:
    """Case-insensitive substring swap, used for account/entry/description text."""
    text = text or ''
    old_name = (old_name or '').strip()
    new_name = (new_name or '').strip()
    if not text or not old_name or not new_name:
        return text
    lowered = text.lower()
    needle = old_name.lower()
    out = []
    index = 0
    while True:
        found = lowered.find(needle, index)
        if found < 0:
            out.append(text[index:])
            break
        out.append(text[index:found])
        out.append(new_name)
        index = found + len(needle)
    return ''.join(out)


def _cash_flow_party_used(party_id) -> bool:
    try:
        return CashFlowEntry.query.filter(CashFlowEntry.party_id == party_id).count() > 0
    except Exception:
        return True


def _cash_flow_snapshot(entry):
    return {
        'id': entry.id,
        'direction': entry.direction,
        'amount': _money(entry.amount),
        'party_id': entry.party_id,
        'party_name': entry.party_name,
        'party_type': entry.party_type,
        'description': entry.description,
        'is_void': bool(entry.is_void),
    }


def _now():
    from app.services.time_money import pk_now
    return pk_now()


def _actor_name(actor=None):
    if actor is None:
        actor = _current_user()
    return (getattr(actor, 'username', None) or 'system') if actor is not None else 'system'


def _current_user():
    try:
        from flask_login import current_user
        if getattr(current_user, 'is_authenticated', False):
            return current_user
    except Exception:
        pass
    return None


# Aliases used by callers/UI copy.
shift_ledger = shift_supplier_ledger
move_supplier_ledger = shift_supplier_ledger
clean_supplier_ledger = shift_supplier_ledger
