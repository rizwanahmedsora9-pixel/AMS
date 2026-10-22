"""Supplier ledger download: full ledger vs date range, PDF and CSV.

Drives the real ``/download_supplier_ledger`` route and checks the statement
contents, the carried-forward balance for a date range, and input validation.
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
def ledger_supplier(app, client):
    """Opening 1,250 (Jan) · GRN 2,500 (Feb 3) · payment 500 (Feb 5) · GRN 1,000 (Mar 10)."""
    login(client)
    with app.app_context():
        supplier = Supplier(name="RANGE SUPPLIER", phone="03001112222",
                            address="Range street", opening_balance=1250,
                            opening_balance_date=datetime(2026, 1, 1))
        feb = GRN(supplier="RANGE SUPPLIER", manual_bill_no="GRN-FEB-001",
                  date_posted=datetime(2026, 2, 3), note="February delivery")
        feb.items.append(GRNItem(mat_name="FEB CEMENT", qty=10, price_at_time=250))
        mar = GRN(supplier="RANGE SUPPLIER", manual_bill_no="GRN-MAR-009",
                  date_posted=datetime(2026, 3, 10))
        mar.items.append(GRNItem(mat_name="MAR SAND", qty=4, price_at_time=250))
        db.session.add_all([supplier, feb, mar])
        db.session.flush()
        feb.supplier_id = supplier.id
        mar.supplier_id = supplier.id
        db.session.add(SupplierPayment(
            supplier_id=supplier.id, amount=500, method="Bank",
            manual_bill_no="PAY-FEB-001", date_posted=datetime(2026, 2, 5),
            note="February payment"))
        db.session.commit()
        return supplier.id


def test_full_ledger_pdf_is_a_professional_statement(client, ledger_supplier):
    resp = client.get(f"/download_supplier_ledger/{ledger_supplier}?action=pdf&scope=full")
    assert resp.status_code == 200
    assert resp.data[:5] == b"%PDF-"
    assert "attachment" in resp.headers["Content-Disposition"]
    text = _pdf_text(resp.data)
    for needle in ("SUPPLIER LEDGER", "RANGE SUPPLIER", "GRN-FEB-001", "GRN-MAR-009",
                   "PAY-FEB-001", "FEB CEMENT", "February payment", "Totals for period",
                   "Authorised sign", "Page 1 of 1",
                   # opening 1,250 + 2,500 - 500 + 1,000 = 4,250
                   "4,250.00"):
        assert needle in text, f"{needle!r} missing from full-ledger PDF"
    # Application chrome must never leak into the statement.
    assert "Sidebar" not in text and "Logout" not in text


def test_date_range_pdf_carries_balance_and_excludes_outside_rows(client, ledger_supplier):
    resp = client.get(
        f"/download_supplier_ledger/{ledger_supplier}"
        "?action=pdf&scope=range&start_date=2026-02-01&end_date=2026-02-28")
    assert resp.status_code == 200
    assert resp.data[:5] == b"%PDF-"
    text = _pdf_text(resp.data)
    # Inside the range
    assert "GRN-FEB-001" in text and "PAY-FEB-001" in text
    # Outside the range (March GRN) must not appear
    assert "GRN-MAR-009" not in text and "MAR SAND" not in text
    assert "Feb 2026" in text or "01 Feb 2026" in text
    # b/f 1,250 + bill 2,500 - payment 500 = closing 3,250
    assert "Balance brought forward" in text
    assert "1,250.00" in text
    assert "3,250.00" in text


def test_range_pdf_open_ended_is_accepted(client, ledger_supplier):
    resp = client.get(f"/download_supplier_ledger/{ledger_supplier}?action=pdf&scope=range&start_date=2026-03-01")
    assert resp.status_code == 200
    assert resp.data[:5] == b"%PDF-"


def test_full_scope_ignores_on_screen_filters(client, ledger_supplier):
    """The download dialog says on-screen filters are not applied to exports."""
    resp = client.get(f"/download_supplier_ledger/{ledger_supplier}?action=download&scope=full&q=nothing-matches")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "GRN-FEB-001" in body and "GRN-MAR-009" in body


def test_csv_uses_day_first_dates_and_bom(client, ledger_supplier):
    resp = client.get(f"/download_supplier_ledger/{ledger_supplier}?action=download&scope=full")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True)
    assert body.startswith("\ufeff")
    assert "03-02-2026" in body and "10-03-2026" in body
    assert "GRN-FEB-001" in body


def test_range_csv_limits_rows_to_period(client, ledger_supplier):
    resp = client.get(
        f"/download_supplier_ledger/{ledger_supplier}"
        "?action=download&scope=range&start_date=2026-02-01&end_date=2026-02-28")
    body = resp.get_data(as_text=True)
    assert "GRN-FEB-001" in body
    assert "GRN-MAR-009" not in body


@pytest.mark.parametrize("query, message", [
    ("scope=range&start_date=2026-02-30&end_date=2026-03-01", "From date is not a valid date."),
    ("scope=range&start_date=2026-03-10&end_date=2026-02-01", "From date cannot be after To date."),
    ("scope=range&start_date=2026-02-01&end_date=bogus", "To date is not a valid date."),
])
def test_invalid_range_is_rejected_with_message(client, ledger_supplier, query, message):
    resp = client.get(f"/download_supplier_ledger/{ledger_supplier}?action=pdf&{query}")
    assert resp.status_code == 400
    assert message in resp.get_data(as_text=True)
