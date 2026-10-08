"""Tests for GRN date/time stamp handling and sorting newest on top."""
from __future__ import annotations

import pytest
from datetime import datetime
from app import create_app
from models import db, Supplier, Material, GRN, Entry


def _login(client, username="Admin", password="Admin@fbm12345"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)


def test_grn_records_date_and_time(client):
    _login(client)

    # 1. Create a supplier and material
    with client.application.app_context():
        sup = Supplier(name="Test DateTime Vendor", phone="111222", is_active=True)
        mat = Material(code="MAT-DT-01", name="Test DateTime Cement", total=0.0)
        db.session.add_all([sup, mat])
        db.session.commit()
        sup_id = sup.id

    # 2. Add GRN with specific date and time
    resp = client.post("/grn", data={
        "action": "add",
        "supplier": "Test DateTime Vendor",
        "supplier_id": str(sup_id),
        "date": "2026-10-05",
        "time": "14:45",
        "payment_type": "Credit",
        "manual_bill_no": "BILL-DT-01",
        "mat_name[]": ["Test DateTime Cement"],
        "qty[]": ["50"],
        "price[]": ["600"],
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"GRN added successfully" in resp.data

    with client.application.app_context():
        grn = GRN.query.filter(GRN.manual_bill_no.like("%BILL-DT-01%")).first()
        assert grn is not None
        assert grn.date_posted.year == 2026
        assert grn.date_posted.month == 10
        assert grn.date_posted.day == 5
        assert grn.date_posted.hour == 14
        assert grn.date_posted.minute == 45

        # Check stock entry has date and time
        entry = Entry.query.filter(Entry.bill_no.like("%BILL-DT-01%")).first()
        assert entry is not None
        assert entry.date == "2026-10-05"
        assert entry.time.startswith("14:45")


def test_grn_sorting_newest_on_top(client):
    _login(client)

    with client.application.app_context():
        sup = Supplier(name="Sorting Vendor", phone="333444", is_active=True)
        mat = Material(code="MAT-SORT-01", name="Sorting Cement", total=0.0)
        db.session.add_all([sup, mat])
        db.session.commit()
        sup_id = sup.id

    # Add three GRNs with different dates/times
    entries = [
        ("SORT-01", "2026-10-01", "10:00"),
        ("SORT-02", "2026-10-03", "09:30"),
        ("SORT-03", "2026-10-03", "16:15"),
    ]

    for bill_no, dt, tm in entries:
        resp = client.post("/grn", data={
            "action": "add",
            "supplier": "Sorting Vendor",
            "supplier_id": str(sup_id),
            "date": dt,
            "time": tm,
            "payment_type": "Credit",
            "manual_bill_no": bill_no,
            "mat_name[]": ["Sorting Cement"],
            "qty[]": ["10"],
            "price[]": ["500"],
        }, follow_redirects=True)
        assert resp.status_code == 200

    # 1. Verify directly in query ordering
    with client.application.app_context():
        sorted_grns = GRN.query.order_by(GRN.date_posted.desc(), GRN.id.desc()).all()
        bill_order = [g.manual_bill_no for g in sorted_grns]
        assert any("SORT-03" in b for b in bill_order[:1]), f"Expected SORT-03 to be newest on top, got {bill_order}"
        idx_03 = next(i for i, b in enumerate(bill_order) if "SORT-03" in b)
        idx_02 = next(i for i, b in enumerate(bill_order) if "SORT-02" in b)
        idx_01 = next(i for i, b in enumerate(bill_order) if "SORT-01" in b)
        assert idx_03 < idx_02 < idx_01, "Expected order: SORT-03 (16:15), SORT-02 (09:30), SORT-01"

    # 2. Verify in HTML table
    get_resp = client.get("/grn")
    assert get_resp.status_code == 200

    html = get_resp.data.decode("utf-8")
    records_start = html.find("GRN Records")
    assert records_start != -1

    pos_03 = html.find("SORT-03", records_start)
    pos_02 = html.find("SORT-02", records_start)
    pos_01 = html.find("SORT-01", records_start)

    assert pos_03 != -1 and pos_02 != -1 and pos_01 != -1
    assert pos_03 < pos_02 < pos_01, f"Expected newest GRN (SORT-03) on top in HTML table: pos_03={pos_03}, pos_02={pos_02}, pos_01={pos_01}"
