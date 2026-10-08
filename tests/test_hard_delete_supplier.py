"""Tests for hard deleting a supplier and wiping their wrong entries."""
from __future__ import annotations

import pytest
from app import create_app
from models import (
    db, Supplier, SupplierPayment, GRN, GRNItem, Entry, Material, Account, AccountTransaction, User
)
from app.services.void_rebuild import hard_delete_supplier


def _login(client, username="Admin", password="Admin@fbm12345"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)


def test_hard_delete_supplier_without_transactions():
    app = create_app()
    with app.app_context():
        # Create test supplier
        s = Supplier(name="Test Typo Supplier", phone="123456", opening_balance=5000, is_active=True)
        db.session.add(s)
        db.session.commit()
        s_id = s.id

        # Hard delete
        res = hard_delete_supplier(s_id)
        db.session.commit()
        assert res["supplier_name"] == "Test Typo Supplier"
        assert db.session.get(Supplier, s_id) is None

        # Verify we can re-add with the exact same name without duplicate error
        s2 = Supplier(name="Test Typo Supplier", phone="999999", opening_balance=0, is_active=True)
        db.session.add(s2)
        db.session.commit()
        assert s2.id is not None
        assert s2.name == "Test Typo Supplier"


def test_hard_delete_supplier_with_grn_and_payments(client):
    app = client.application
    _login(client)

    with app.app_context():
        # Create cash account
        acc = Account(name="Test Cash Account", category="cash", account_type="cash", balance=50000.0, is_active=True)
        mat = Material(code="MAT-HD-001", name="Test Hard Delete Cement", total=0.0)
        s = Supplier(name="Fauji Cement Vendor", phone="03001234567", opening_balance=10000.0, is_active=True)
        db.session.add_all([acc, mat, s])
        db.session.commit()
        s_id = s.id
        acc_id = acc.id
        initial_balance = acc.balance

        # 1. Create a GRN for this supplier
        grn = GRN(
            supplier=s.name,
            supplier_id=s.id,
            manual_bill_no="GRN-HD-001",
            auto_bill_no="EN-HD-001",
            paid_amount=15000.0,
            payment_type="Cash",
            payment_account_id=acc_id,
        )
        db.session.add(grn)
        db.session.flush()

        # Add GRN item and update material total
        grn_item = GRNItem(grn_id=grn.id, mat_name=mat.name, qty=100.0, price_at_time=500.0)
        mat.total += 100.0
        stock_entry = Entry(
            type="IN",
            material=mat.name,
            client=s.name,
            qty=100.0,
            auto_bill_no=grn.auto_bill_no,
            bill_no=grn.manual_bill_no,
        )
        db.session.add_all([grn_item, stock_entry])

        # Auto supplier payment for GRN
        from app.services.grn_svc import _sync_grn_auto_supplier_payment
        _sync_grn_auto_supplier_payment(grn)
        db.session.commit()

        # Deduct paid amount from account to simulate realistic state
        acc = db.session.get(Account, acc_id)
        assert acc.balance < initial_balance

        # 2. Add a standalone supplier payment
        from app.services.payments_crud import save_supplier_payment
        standalone_pay, _ = save_supplier_payment(
            supplier_id=s_id,
            amount=5000.0,
            method="Cash",
            payment_account_id=acc_id,
            actor=None,
        )
        db.session.commit()

        # Verify supplier has entries
        assert GRN.query.filter_by(supplier_id=s_id).count() == 1
        assert SupplierPayment.query.filter_by(supplier_id=s_id).count() >= 2
        assert db.session.get(Material, mat.id).total == 100.0

    # 3. Post to hard_delete_supplier endpoint via client
    resp = client.post(f"/hard_delete_supplier/{s_id}", data={"action": "hard_delete"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"permanently deleted" in resp.data

    with app.app_context():
        # Verify supplier is completely gone
        assert db.session.get(Supplier, s_id) is None
        # Verify all GRNs for this supplier are gone
        assert GRN.query.filter_by(supplier_id=s_id).count() == 0
        # Verify all SupplierPayments for this supplier are gone
        assert SupplierPayment.query.filter_by(supplier_id=s_id).count() == 0
        # Verify material total was reversed to 0
        mat_after = Material.query.filter_by(name="Test Hard Delete Cement").first()
        assert mat_after.total == 0.0
        # Verify stock entry was removed
        assert Entry.query.filter_by(auto_bill_no="EN-HD-001").count() == 0

        # Verify supplier can be re-added from scratch cleanly
        readd_resp = client.post("/add_supplier", data={
            "name": "Fauji Cement Vendor",
            "phone": "03009999999",
            "opening_balance": 0.0,
        }, follow_redirects=True)
        assert b"Supplier Added" in readd_resp.data
        new_sup = Supplier.query.filter_by(name="Fauji Cement Vendor").first()
        assert new_sup is not None
        assert new_sup.phone == "03009999999"
        assert new_sup.opening_balance == 0.0
