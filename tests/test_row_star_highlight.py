import pytest
from app import create_app
from models import db, Supplier, Material, GRN, GRNItem
from datetime import datetime


def _login(client, username="Admin", password="Admin@fbm12345"):
    client.post("/login", data={"username": username, "password": password}, follow_redirects=True)


def test_grn_and_ledger_star_highlight_rendered(client, app):
    _login(client)

    with app.app_context():
        supplier = Supplier.query.filter_by(name="Star Supplier").first()
        if not supplier:
            supplier = Supplier(name="Star Supplier", phone="03001234567")
            db.session.add(supplier)
            db.session.commit()

        mat = Material.query.filter_by(name="Star Material").first()
        if not mat:
            mat = Material(code="MAT-STAR-1", name="Star Material", unit="KG", unit_price=200)
            db.session.add(mat)
            db.session.commit()

        grn = GRN(
            supplier=supplier.name,
            supplier_id=supplier.id,
            manual_bill_no="STAR-99",
            auto_bill_no="GRN-STAR-99",
            date_posted=datetime(2026, 4, 1, 10, 0, 0),
        )
        db.session.add(grn)
        db.session.commit()

        entry = GRNItem(
            grn_id=grn.id,
            mat_name=mat.name,
            qty=5,
            price_at_time=200,
        )
        db.session.add(entry)
        db.session.commit()
        supplier_id = supplier.id

    # Test GRN page
    resp_grn = client.get('/grn')
    assert resp_grn.status_code == 200
    grn_html = resp_grn.get_data(as_text=True)
    assert 'row-star-btn' in grn_html
    assert 'entry-highlighted' in grn_html
    assert 'ams_starred_grn_entries' in grn_html

    # Test Supplier Ledger page
    resp_ledger = client.get(f'/supplier_ledger/{supplier_id}')
    assert resp_ledger.status_code == 200
    ledger_html = resp_ledger.get_data(as_text=True)
    assert 'row-star-btn' in ledger_html
    assert 'entry-highlighted' in ledger_html
    assert 'ams_starred_ledger_' in ledger_html
