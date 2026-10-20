from __future__ import annotations

import pytest
from models import db, GRN, GRNItem, Entry, Material, Supplier

LOGIN = {"username": "Admin", "password": "Admin@fbm12345"}


def login(client):
    resp = client.post("/login", data=LOGIN, follow_redirects=True)
    assert resp.status_code == 200
    return resp


def test_grn_multiple_items_saves_all_and_creates_stock_entries(client, app):
    login(client)

    with app.app_context():
        sup = Supplier(name="Multi Item Supplier", is_active=True)
        mat1 = Material(code="MAT-T1", name="Cement OPC", total=0.0)
        mat2 = Material(code="MAT-T2", name="Cement SRC", total=0.0)
        db.session.add_all([sup, mat1, mat2])
        db.session.commit()
        sup_id = sup.id

    resp = client.post("/grn", data={
        "action": "add",
        "supplier": "Multi Item Supplier",
        "supplier_id": str(sup_id),
        "date": "2026-10-20",
        "time": "10:30",
        "payment_type": "Credit",
        "manual_bill_no": "MULTI-001",
        "mat_name[]": ["Cement OPC", "Cement SRC"],
        "qty[]": ["50", "30"],
        "price[]": ["600", "650"],
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"GRN added successfully" in resp.data

    with app.app_context():
        grn = GRN.query.first()
        assert grn is not None, "GRN record should be created"
        items = GRNItem.query.filter_by(grn_id=grn.id).all()
        assert len(items) == 2, f"Expected 2 GRN items, got {len(items)}"
        assert {it.mat_name: it.qty for it in items} == {"Cement OPC": 50.0, "Cement SRC": 30.0}

        entries = Entry.query.filter_by(auto_bill_no=grn.auto_bill_no, type="IN").all()
        assert len(entries) == 2, f"Expected 2 stock entries for the 2 items, got {len(entries)}"
        assert {e.material: e.qty for e in entries} == {"Cement OPC": 50.0, "Cement SRC": 30.0}


def test_grn_add_does_not_truncate_items_when_optional_price_is_missing(client, app):
    """A shorter optional price list must not make zip() drop later items."""
    login(client)

    with app.app_context():
        sup = Supplier(name="Missing Rate Supplier", is_active=True)
        mat1 = Material(code="MAT-MR1", name="Rate Cement A", total=0.0)
        mat2 = Material(code="MAT-MR2", name="Rate Cement B", total=0.0)
        db.session.add_all([sup, mat1, mat2])
        db.session.commit()
        sup_id = sup.id

    resp = client.post("/grn", data={
        "action": "add",
        "supplier": "Missing Rate Supplier",
        "supplier_id": str(sup_id),
        "date": "2026-10-20",
        "time": "10:30",
        "payment_type": "Credit",
        "manual_bill_no": "MULTI-MISSING-RATE",
        "mat_name[]": ["Rate Cement A", "Rate Cement B"],
        "qty[]": ["50", "30"],
        # The rate is optional; if its second posted value is missing, it must
        # default to zero without truncating the second material/quantity pair.
        "price[]": ["600"],
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"GRN added successfully" in resp.data

    with app.app_context():
        grn = GRN.query.one()
        items = GRNItem.query.filter_by(grn_id=grn.id).all()
        assert len(items) == 2
        assert {it.mat_name: (it.qty, it.price_at_time) for it in items} == {
            "Rate Cement A": (50.0, 600.0),
            "Rate Cement B": (30.0, 0.0),
        }
        entries = Entry.query.filter_by(auto_bill_no=grn.auto_bill_no, type="IN").all()
        assert len(entries) == 2
        assert {entry.material: entry.qty for entry in entries} == {
            "Rate Cement A": 50.0,
            "Rate Cement B": 30.0,
        }
