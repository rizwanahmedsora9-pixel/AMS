"""Supplier ledger PDF: every GRN line shows its quantity, rate and amount.

The statement used to print only the value of a bill, so a GRN printed as one
lump credit with no hint of what was received.  Quantity, Rate and Amount are
now real columns filled from the GRN line items, while a payment keeps those
columns empty and carries its amount in the debit column.
"""
from __future__ import annotations

import io
from datetime import datetime

import pytest

from models import db, Supplier, GRN, GRNItem, SupplierPayment


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def _pdf_text(data):
    pytest.importorskip("pypdf")
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


@pytest.fixture()
def item_supplier(app, client):
    """Opening 1,250 · GRN 6,443.75 with two material lines · payment 500.

    Line 1: 12.5 x 275.50 = 3,443.75     Line 2: 3 x 1,000 = 3,000
    """
    login(client)
    with app.app_context():
        supplier = Supplier(name="ITEM DETAIL SUPPLIER", phone="03007778888",
                            opening_balance=1250,
                            opening_balance_date=datetime(2026, 1, 1))
        grn = GRN(supplier="ITEM DETAIL SUPPLIER", manual_bill_no="GRN-ITEM-001",
                  date_posted=datetime(2026, 2, 3), note="Two materials delivered")
        grn.items.append(GRNItem(mat_name="DETAIL CEMENT", qty=12.5, price_at_time=275.5))
        grn.items.append(GRNItem(mat_name="DETAIL SAND", qty=3, price_at_time=1000))
        db.session.add_all([supplier, grn])
        db.session.flush()
        grn.supplier_id = supplier.id
        db.session.add(SupplierPayment(
            supplier_id=supplier.id, amount=500, method="Bank",
            manual_bill_no="PAY-ITEM-001", date_posted=datetime(2026, 2, 5)))
        db.session.commit()
        return supplier.id


def test_pdf_has_quantity_rate_and_amount_columns(client, item_supplier):
    resp = client.get(f"/download_supplier_ledger/{item_supplier}?action=pdf&scope=full")
    assert resp.status_code == 200
    assert resp.data[:5] == b"%PDF-"
    text = _pdf_text(resp.data)
    for header in ("QTY", "RATE", "AMOUNT", "DEBIT", "CREDIT", "BALANCE"):
        assert header in text, f"{header} column missing from the supplier ledger PDF"


def test_grn_lines_print_their_own_quantity_rate_and_amount(client, item_supplier):
    resp = client.get(f"/download_supplier_ledger/{item_supplier}?action=pdf&scope=full")
    text = _pdf_text(resp.data)
    # Materials, quantities, rates and line amounts of both GRN lines.
    for needle in ("DETAIL CEMENT", "DETAIL SAND", "12.5", "275.5", "3,443.75",
                   "1,000", "3,000"):
        assert needle in text, f"{needle!r} missing from the GRN line detail"
    # The bill itself is still a single credit of 6,443.75 and the running
    # balance is unchanged: 1,250 opening + 6,443.75 - 500 paid.
    assert "6,443.75" in text
    assert "7,193.75" in text


def test_payment_row_keeps_quantity_columns_empty(client, item_supplier):
    """A payment has no material lines: the amount belongs to debit only."""
    resp = client.get(f"/download_supplier_ledger/{item_supplier}?action=pdf&scope=full")
    text = _pdf_text(resp.data)
    assert "PAY-ITEM-001" in text
    assert "Payment (Bank)" in text
    # 500 is the payment, and it is never rendered as a quantity or a rate.
    assert "500.00" in text
    assert "500 x" not in text


def test_csv_export_still_matches_the_previous_contract(client, item_supplier):
    resp = client.get(f"/download_supplier_ledger/{item_supplier}?action=download&scope=full")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    header = body.splitlines()[0]
    assert header.strip("\ufeff") == "Date,Type,Reference,Description,Debit,Credit,Balance,Account,Notes"
    assert "GRN-ITEM-001" in body


def test_date_range_pdf_keeps_item_detail_and_carry_forward(client, item_supplier):
    resp = client.get(
        f"/download_supplier_ledger/{item_supplier}"
        "?action=pdf&scope=range&start_date=2026-02-01&end_date=2026-02-28")
    assert resp.status_code == 200
    text = _pdf_text(resp.data)
    assert "Balance brought forward" in text
    assert "DETAIL CEMENT" in text and "275.5" in text
    assert "1,250.00" in text
