"""Full supplier-ledger shift: every entry (GRNs, payments, opening balance,
accounts, cash flow) moves and the Accounts section follows.

These tests drive the real service + HTTP layer and assert the ground truth in
the ORM (no service helper is used to verify itself).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from conftest import make_csrf_client

from models import (
    db,
    Account,
    AccountTransaction,
    CashFlowEntry,
    CashFlowParty,
    Entry,
    GRN,
    GRNItem,
    Material,
    Supplier,
    SupplierPayment,
)
from app.services.supplier_ledger_shift import (
    preview_supplier_ledger_shift,
    reclaim_supplier_ledger,
    shift_supplier_ledger,
    shift_supplier_payment_to_supplier,
)


def _login(client, username="Admin", password="Admin@fbm12345"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)


def _seed_two_suppliers(app):
    """A wrong supplier with a GRN, payments, opening balance, linked account
    and a manual cash-flow row, plus the correct supplier."""
    with app.app_context():
        cash = Account(name="Shift Test Cash", category="cash", account_type="cash",
                       balance=100000.0, is_active=True)
        material = Material(code="MAT-SHIFT-1", name="Shift Test Cement", total=0.0)
        wrong = Supplier(name="Wrong Supplier Ltd", phone="0300-0000001",
                         opening_balance=2500.0, is_active=True)
        right = Supplier(name="Correct Supplier Ltd", phone="0300-0000002",
                         opening_balance=0.0, is_active=True)
        db.session.add_all([cash, material, wrong, right])
        db.session.commit()

        cash_id, material_id = cash.id, material.id
        wrong_id, right_id = wrong.id, right.id

        # A consumed (locked) GRN lot: must survive the shift untouched.
        grn = GRN(supplier=wrong.name, supplier_id=wrong.id, manual_bill_no="GRN-SHIFT-1",
                  auto_bill_no="SB-EN-1", paid_amount=4000.0, payment_type="Cash",
                  payment_account_id=cash_id, date_posted=datetime(2026, 1, 10, 9, 0, 0))
        db.session.add(grn)
        db.session.flush()
        db.session.add(GRNItem(grn_id=grn.id, mat_name=material.name, qty=50.0,
                               price_at_time=500.0, is_locked=True))
        db.session.add(Entry(type="IN", material=material.name, client=wrong.name,
                             qty=50.0, auto_bill_no="SB-EN-1",
                             bill_no=grn.manual_bill_no))
        db.session.get(Material, material_id).total = 50.0
        db.session.commit()
        grn_id = grn.id

        from app.services.grn_svc import _sync_grn_auto_supplier_payment
        _sync_grn_auto_supplier_payment(grn)
        db.session.commit()

        # A standalone payment plus a voided one (both must move).
        from app.services.payments_crud import save_supplier_payment
        standalone, _ = save_supplier_payment(
            supplier_id=wrong_id, amount=1500.0, method="Cash",
            payment_account_id=cash_id, note="Standalone shift payment", actor=None,
        )
        db.session.commit()
        voided, _ = save_supplier_payment(
            supplier_id=wrong_id, amount=700.0, method="Cash",
            payment_account_id=cash_id, note="Voided shift payment", actor=None,
        )
        db.session.commit()
        from app.services.payments_crud import delete_supplier_payment
        delete_supplier_payment(voided, actor=None)
        db.session.commit()

        # An account linked to the wrong supplier (Accounts section link).
        supplier_account = Account(
            name=f"Payable - {wrong.name}", category="cash", account_type="supplier",
            type="supplier", balance=0.0, is_active=True,
            linked_entity_type="supplier", linked_supplier_id=wrong_id,
            linked_party_name=wrong.name,
        )
        db.session.add(supplier_account)

        # A manual cash-flow row recorded against the wrong supplier's party.
        party = CashFlowParty(name=wrong.name, party_type="supplier", is_active=True)
        db.session.add(party)
        db.session.flush()
        cf_entry = CashFlowEntry(
            direction="out", amount=300.0, account_id=cash_id, party_id=party.id,
            party_name=wrong.name, party_type="supplier",
            description=f"Advance — {wrong.name}", source_type="MANUAL_CASH_FLOW",
        )
        db.session.add(cf_entry)
        db.session.commit()

        ids = {
            'cash': cash_id, 'material': material_id, 'wrong': wrong_id, 'right': right_id,
            'grn': grn_id, 'standalone_payment': standalone.id, 'voided_payment': voided.id,
            'supplier_account': supplier_account.id, 'cf_entry': cf_entry.id, 'party': party.id,
        }
        return ids, cash.balance


def _supplier_ledger_rows(supplier_id):
    from app.services.financial_ledgers import build_supplier_financial_ledger
    supplier = db.session.get(Supplier, supplier_id)
    return build_supplier_financial_ledger(supplier)['rows']


def test_shift_moves_every_ledger_entry_and_keeps_accounts_in_step(app):
    ids = _seed_two_suppliers(app)[0]

    with app.app_context():
        wrong = db.session.get(Supplier, ids['wrong'])
        right = db.session.get(Supplier, ids['right'])
        cash_before = db.session.get(Account, ids['cash']).balance

        preview = preview_supplier_ledger_shift(wrong, right)
        assert preview['grns'] == 1
        assert preview['payments'] == 3  # 1 auto + 1 standalone + 1 voided
        assert preview['voided_payments'] == 1
        assert preview['opening_balance_movable'] == 2500.0
        assert preview['legacy_entries'] == 1
        assert len(preview['linked_accounts']) == 1
        assert preview['cash_flow_entries'] == 1
        assert len(preview['locked_grns']) == 1
        assert 'GRN-SHIFT-1' in preview['locked_grns'][0]
        assert preview['source']['ledger_rows'] > 0

        result = shift_supplier_ledger(wrong, right, actor=None)
        db.session.commit()

    with app.app_context():
        wrong = db.session.get(Supplier, ids['wrong'])
        right = db.session.get(Supplier, ids['right'])

        # 1. The old supplier ledger is completely empty.
        assert _supplier_ledger_rows(ids['wrong']) == []
        assert result['source_rows_after'] == 0

        # 2. Everything now belongs to the new supplier.
        grn = db.session.get(GRN, ids['grn'])
        assert grn.supplier_id == ids['right'] and grn.supplier == right.name
        assert GRN.query.filter_by(supplier_id=ids['wrong']).count() == 0
        assert SupplierPayment.query.filter_by(supplier_id=ids['wrong']).count() == 0
        moved_payments = SupplierPayment.query.filter_by(supplier_id=ids['right']).all()
        assert {p.id for p in moved_payments} >= {
            ids['standalone_payment'], ids['voided_payment'],
        }
        assert sum(1 for p in moved_payments if p.is_void) == 1

        # 3. Opening balance moved (adds onto the target, zeroes the source).
        assert wrong.opening_balance == 0.0
        assert right.opening_balance == 2500.0

        # 4. Legacy stock row renamed; inventory untouched.
        entry = Entry.query.filter_by(type='IN', auto_bill_no='SB-EN-1').one()
        assert entry.client == right.name
        assert db.session.get(Material, ids['material']).total == 50.0
        item = GRNItem.query.filter_by(grn_id=ids['grn']).one()
        assert item.is_locked is True

        # 5. Accounts section: the linked account follows and is renamed.
        account = db.session.get(Account, ids['supplier_account'])
        assert account.linked_supplier_id == ids['right']
        assert account.linked_party_name == right.name
        assert account.name == f"Payable - {right.name}"

        # 6. Cash/bank transactions now name the new supplier.
        moved_payment_ids = [p.id for p in moved_payments]
        txs = AccountTransaction.query.filter(
            AccountTransaction.source_type == 'SupplierPayment',
            AccountTransaction.source_id.in_(moved_payment_ids),
        ).all()
        assert txs, 'supplier payment transactions missing'
        assert all(right.name in (tx.description or '') for tx in txs if not tx.is_void)
        active_amounts = sum(abs(float(tx.amount or 0)) for tx in txs if not tx.is_void)
        assert active_amounts == pytest.approx(4000.0 + 1500.0, rel=1e-6)

        # 7. Cash-flow row + party moved to the new supplier.
        cf_entry = db.session.get(CashFlowEntry, ids['cf_entry'])
        assert cf_entry.party_name == right.name
        assert right.name in (cf_entry.description or '')
        party = db.session.get(CashFlowParty, cf_entry.party_id)
        assert party.name == right.name

        # 8. Source is suspended + marked transferred; ledger total is unchanged
        #    overall (money never changed hands).
        assert wrong.is_active is False
        assert wrong.transferred_to_id == ids['right']
        assert result['target_closing_after'] == pytest.approx(
            result['source_closing_before'] + result['target_closing_before'], rel=1e-6
        )
        cash_after = db.session.get(Account, ids['cash']).balance
        assert cash_after == pytest.approx(cash_before, rel=1e-9)


def test_shift_to_brand_new_supplier_and_reclaim(app):
    ids = _seed_two_suppliers(app)[0]

    with app.app_context():
        wrong = db.session.get(Supplier, ids['wrong'])
        result = shift_supplier_ledger(wrong, "Fresh Supplier Ltd", actor=None)
        db.session.commit()
        new_id = result['target_supplier_id']
        assert result['target_was_new'] is True
        new_supplier = db.session.get(Supplier, new_id)
        assert new_supplier is not None and new_supplier.is_active

    with app.app_context():
        assert _supplier_ledger_rows(ids['wrong']) == []
        assert len(_supplier_ledger_rows(new_id)) > 0
        accounts = Account.query.filter_by(linked_supplier_id=new_id).count()
        assert accounts == 1

        wrong = db.session.get(Supplier, ids['wrong'])
        reclaim = reclaim_supplier_ledger(wrong, actor=None)
        db.session.commit()
        assert reclaim['grns_reclaimed'] == 1
        assert reclaim['payments_reclaimed'] == 3

    with app.app_context():
        wrong = db.session.get(Supplier, ids['wrong'])
        assert wrong.is_active is True and wrong.transferred_to_id is None
        assert wrong.opening_balance == 2500.0
        assert GRN.query.filter_by(supplier_id=ids['wrong']).count() == 1
        assert SupplierPayment.query.filter_by(supplier_id=ids['wrong']).count() == 3
        assert _supplier_ledger_rows(ids['wrong'])

        account = db.session.get(Account, ids['supplier_account'])
        assert account.linked_supplier_id == ids['wrong']
        assert account.name == "Payable - Wrong Supplier Ltd"
        entry = Entry.query.filter_by(auto_bill_no='SB-EN-1').one()
        assert entry.client == "Wrong Supplier Ltd"

        from app.services.financial_ledgers import build_supplier_financial_ledger
        new_supplier = Supplier.query.filter_by(name="Fresh Supplier Ltd").one()
        assert build_supplier_financial_ledger(new_supplier)['rows'] == []


def test_shift_endpoint_and_single_payment_endpoint(app):
    with app.app_context():
        cash = Account(name="Endp Cash", category="cash", account_type="cash",
                       balance=50000.0, is_active=True)
        source = Supplier(name="Endpoint Wrong", opening_balance=0.0, is_active=True)
        target = Supplier(name="Endpoint Right", opening_balance=0.0, is_active=True)
        db.session.add_all([cash, source, target])
        db.session.commit()
        cash_id, source_id, target_id = cash.id, source.id, target.id

        from app.services.payments_crud import save_supplier_payment
        payment, _ = save_supplier_payment(
            supplier_id=source_id, amount=900.0, method="Cash",
            payment_account_id=cash_id, note="Endpoint payment", actor=None,
        )
        db.session.commit()
        payment_id = payment.id

    client = make_csrf_client(app)
    _login(client)

    # Single-row shift: a standalone payment can be re-pointed on its own.
    resp = client.post(f"/shift_supplier_payment/{payment_id}", data={
        "target_supplier_id": str(target_id),
        "return_to": "/suppliers",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"moved from" in resp.data
    with app.app_context():
        assert db.session.get(SupplierPayment, payment_id).supplier_id == target_id
        tx = AccountTransaction.query.filter_by(
            source_type='SupplierPayment', source_id=payment_id).one()
        assert "Endpoint Right" in (tx.description or '')

    # Full shift through the HTTP endpoint (preview page + confirm post).
    with app.app_context():
        from app.services.payments_crud import save_supplier_payment
        save_supplier_payment(supplier_id=target_id, amount=100.0, method="Cash",
                              payment_account_id=cash_id, actor=None)
        db.session.commit()

    page = client.get(f"/supplier_ledger_shift/{source_id}")
    assert page.status_code == 200
    assert b"Shift entire ledger" in page.data

    # The pages that host the new controls must still render.
    for url, needle in (
        ("/suppliers", b"Shift Entire Ledger"),
        (f"/supplier_ledger/{source_id}", b"Shift entire ledger"),
        ("/accounts/payments/suppliers", b"Shift to another supplier"),
        (f"/api/supplier_ledger_shift_preview/{source_id}", b"preview"),
    ):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert needle in resp.data, url

    post = client.post(f"/shift_supplier_ledger/{source_id}", data={
        "target_supplier_id": str(target_id),
        "move_opening_balance": "1",
        "move_legacy_entries": "1",
        "move_cash_flow": "1",
        "relink_accounts": "1",
        "rename_accounts": "1",
        "deactivate_source": "1",
        "confirm": "1",
    }, follow_redirects=True)
    assert post.status_code == 200
    assert b"Shifted" in post.data

    with app.app_context():
        assert SupplierPayment.query.filter_by(supplier_id=source_id).count() == 0
        assert db.session.get(Supplier, source_id).is_active is False
        assert db.session.get(Supplier, source_id).transferred_to_id == target_id


def test_shift_is_transactional_on_failure(app):
    """A failed shift must not leave half-moved rows behind."""
    ids = _seed_two_suppliers(app)[0]

    with app.app_context():
        wrong = db.session.get(Supplier, ids['wrong'])
        same = db.session.get(Supplier, ids['wrong'])
        with pytest.raises(ValueError):
            shift_supplier_ledger(wrong, same, actor=None)
        db.session.rollback()

    with app.app_context():
        assert GRN.query.filter_by(supplier_id=ids['wrong']).count() == 1
        assert SupplierPayment.query.filter_by(supplier_id=ids['wrong']).count() == 3
        assert db.session.get(Supplier, ids['wrong']).opening_balance == 2500.0
        assert db.session.get(Supplier, ids['wrong']).is_active is True


def test_grn_auto_payment_single_shift_is_refused_with_guidance(app):
    ids = _seed_two_suppliers(app)[0]
    with app.app_context():
        auto = SupplierPayment.query.filter_by(
            supplier_id=ids['wrong'], source_type='GRN').first()
        assert auto is not None
        with pytest.raises(ValueError) as exc:
            shift_supplier_payment_to_supplier(auto, ids['right'], actor=None)
        assert 'GRN' in str(exc.value)
        db.session.rollback()
