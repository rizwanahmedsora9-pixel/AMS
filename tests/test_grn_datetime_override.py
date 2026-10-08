"""GRN manual date/time override — adding *and* editing.

The GRN wizard must let the operator overrule the clock: a receipt is stamped
with the moment the goods actually arrived, not the moment it was typed.  The
date and time typed by hand must survive add, edit, the GRN list and the
stock-ledger (Entry) rows, and unreadable input must be refused instead of
silently stamping the record with the server clock.
"""
from __future__ import annotations

import re
from datetime import timedelta

from app.services.time_money import pk_now, pk_today
from models import db, GRN, GRNItem, Entry, Material, Client, Supplier

LOGIN = {"username": "Admin", "password": "Admin@fbm12345"}


def login(client):
    resp = client.post("/login", data=LOGIN, follow_redirects=True)
    assert resp.status_code == 200
    return resp


def flash_text(response):
    html = response.get_data(as_text=True)
    msgs = re.findall(r"alert[^>]*>(.{0,400}?)</div>", html, re.S)
    return " | ".join(re.sub(r"<[^>]+>|\s+", " ", m).strip() for m in msgs)


def seed_supplier_material(app, *, supplier="Override Supplier", material="Override Cement"):
    with app.app_context():
        sup = Supplier(name=supplier, is_active=True)
        mat = Material(code=f"MAT-OVR-{material[:3].upper()}", name=material, total=0.0)
        db.session.add_all([sup, mat])
        db.session.commit()
        return sup.id


def add_grn(client, supplier_id, supplier, material, *, date, time, bill, qty="50", price="600"):
    return client.post("/grn", data={
        "action": "add",
        "supplier": supplier,
        "supplier_id": str(supplier_id),
        "date": date,
        "time": time,
        "payment_type": "Credit",
        "manual_bill_no": bill,
        "mat_name[]": [material],
        "qty[]": [qty],
        "price[]": [price],
    }, follow_redirects=True)


def grn_by_bill(bill):
    return GRN.query.filter(GRN.manual_bill_no.like(f"%{bill}%")).first()


def entries_for(grn):
    return Entry.query.filter(Entry.auto_bill_no == grn.auto_bill_no, Entry.type == "IN").all()


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------
def test_add_grn_accepts_manual_12_hour_and_seconds(client, app):
    login(client)
    sup_id = seed_supplier_material(app)

    resp = add_grn(client, sup_id, "Override Supplier", "Override Cement",
                   date="2026-10-05", time="2:45 PM", bill="OVR-A1")
    assert b"GRN added successfully" in resp.data

    resp = add_grn(client, sup_id, "Override Supplier", "Override Cement",
                   date="2026-10-06", time="06:15:30", bill="OVR-A2")
    assert b"GRN added successfully" in resp.data

    with app.app_context():
        first = grn_by_bill("OVR-A1")
        assert (first.date_posted.hour, first.date_posted.minute) == (14, 45), first.date_posted
        assert entries_for(first)[0].time.startswith("14:45")

        second = grn_by_bill("OVR-A2")
        assert (second.date_posted.hour, second.date_posted.minute, second.date_posted.second) == (6, 15, 30)
        assert entries_for(second)[0].time == "06:15:30"


def test_add_grn_refuses_unreadable_time(client, app):
    login(client)
    sup_id = seed_supplier_material(app)

    resp = add_grn(client, sup_id, "Override Supplier", "Override Cement",
                   date="2026-10-05", time="25:99", bill="OVR-BAD")
    text = flash_text(resp)
    assert "not a valid time" in text, text
    with app.app_context():
        assert grn_by_bill("OVR-BAD") is None, "bad time must not create a GRN with the server clock"

    # A bare 4-digit value is a date-looking typo, not a time — ask for a colon
    # instead of silently reading it as 20:26.
    resp = add_grn(client, sup_id, "Override Supplier", "Override Cement",
                   date="2026-10-05", time="2026", bill="OVR-BAD2")
    assert "not a valid time" in flash_text(resp)
    with app.app_context():
        assert grn_by_bill("OVR-BAD2") is None


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------
def test_edit_grn_changes_date_time_and_stock_entry(client, app):
    login(client)
    sup_id = seed_supplier_material(app)
    add_grn(client, sup_id, "Override Supplier", "Override Cement",
            date="2026-10-05", time="14:45", bill="OVR-E1")

    with app.app_context():
        grn = grn_by_bill("OVR-E1")
        grn_id, item_id = grn.id, grn.items[0].id

    resp = client.post(f"/edit_grn/{grn_id}", data={
        "supplier": "Override Supplier",
        "supplier_id": str(sup_id),
        "date": "2026-09-20",
        "time": "6:15 PM",
        "payment_type": "Credit",
        "manual_bill_no": "OVR-E1",
        "grn_item_id[]": [str(item_id)],
        "mat_name[]": ["Override Cement"],
        "qty[]": ["50"],
        "price[]": ["600"],
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "GRN updated successfully" in flash_text(resp)

    with app.app_context():
        grn = db.session.get(GRN, grn_id)
        assert (grn.date_posted.year, grn.date_posted.month, grn.date_posted.day) == (2026, 9, 20)
        assert (grn.date_posted.hour, grn.date_posted.minute) == (18, 15), grn.date_posted
        rows = entries_for(grn)
        assert rows, "edit must rebuild the stock ledger row"
        assert (rows[0].date, rows[0].time) == ("2026-09-20", "18:15:00"), (rows[0].date, rows[0].time)


def test_edit_grn_refuses_unreadable_time(client, app):
    login(client)
    sup_id = seed_supplier_material(app)
    add_grn(client, sup_id, "Override Supplier", "Override Cement",
            date="2026-10-05", time="14:45", bill="OVR-E2")

    with app.app_context():
        grn = grn_by_bill("OVR-E2")
        grn_id, item_id, before = grn.id, grn.items[0].id, grn.date_posted

    resp = client.post(f"/edit_grn/{grn_id}", data={
        "supplier": "Override Supplier",
        "supplier_id": str(sup_id),
        "date": "2026-10-05",
        "time": "not-a-time",
        "payment_type": "Credit",
        "manual_bill_no": "OVR-E2",
        "grn_item_id[]": [str(item_id)],
        "mat_name[]": ["Override Cement"],
        "qty[]": ["50"],
        "price[]": ["600"],
    }, follow_redirects=True)
    text = flash_text(resp)
    assert "not a valid time" in text, text
    with app.app_context():
        grn = db.session.get(GRN, grn_id)
        assert grn.date_posted == before, "stamp must not move on a rejected time"


def test_edit_grn_without_date_time_fields_keeps_stamp(client, app):
    """Programmatic posts that carry no date/time keep the stored stamp."""
    login(client)
    sup_id = seed_supplier_material(app)
    add_grn(client, sup_id, "Override Supplier", "Override Cement",
            date="2026-10-05", time="14:45", bill="OVR-E3")

    with app.app_context():
        grn = grn_by_bill("OVR-E3")
        grn_id, item_id, before = grn.id, grn.items[0].id, grn.date_posted

    resp = client.post(f"/edit_grn/{grn_id}", data={
        "supplier": "Override Supplier",
        "supplier_id": str(sup_id),
        "payment_type": "Credit",
        "manual_bill_no": "OVR-E3",
        "grn_item_id[]": [str(item_id)],
        "mat_name[]": ["Override Cement"],
        "qty[]": ["50"],
        "price[]": ["600"],
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(GRN, grn_id).date_posted == before


def test_date_only_added_in_the_past_lands_at_midnight(client, app):
    """Same rule as resolve_posted_datetime: past date + no time = 00:00."""
    login(client)
    sup_id = seed_supplier_material(app)
    past = (pk_today() - timedelta(days=3)).strftime("%Y-%m-%d")

    resp = add_grn(client, sup_id, "Override Supplier", "Override Cement",
                   date=past, time="", bill="OVR-D1")
    assert b"GRN added successfully" in resp.data
    with app.app_context():
        grn = grn_by_bill("OVR-D1")
        assert grn.date_posted.strftime("%Y-%m-%d") == past
        assert (grn.date_posted.hour, grn.date_posted.minute, grn.date_posted.second) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Locked lots: the GRN stamp may move, the stock ledger must follow
# ---------------------------------------------------------------------------
def test_edit_locked_lot_grn_still_syncs_stock_entry_time(client, app):
    login(client)
    with app.app_context():
        sup = Supplier(name="Locked Supplier", is_active=True)
        mat = Material(code="MAT-LOCK-01", name="Locked Cement", total=0.0)
        cli = Client(name="Locked Client", code="CL-LOCK", category="General", is_active=True)
        db.session.add_all([sup, mat, cli])
        db.session.commit()
        sup_id = sup.id

    past = (pk_today() - timedelta(days=5)).strftime("%Y-%m-%d")
    add_grn(client, sup_id, "Locked Supplier", "Locked Cement",
            date=past, time="09:00", bill="OVR-LOCK", qty="500", price="100")

    # A credit sale consumes (locks) the lot, so only header edits stay open.
    sale = client.post("/add_direct_sale", data={
        "client_name": "Locked Client",
        "driver_name": "Driver",
        "category": "Credit Customer",
        "product_name[]": "Locked Cement",
        "qty[]": "200",
        "unit_rate[]": "150",
        "payment_method": "Credit",
        "paid_amount": "0",
        "manual_bill_no": "MB-LOCK-1",
    }, follow_redirects=True)
    assert sale.status_code == 200

    with app.app_context():
        grn = grn_by_bill("OVR-LOCK")
        assert any(i.is_locked for i in grn.items), "sale must lock the lot"
        grn_id, item_id = grn.id, grn.items[0].id
        stock_before = float(Material.query.filter_by(name="Locked Cement").first().total or 0)

    resp = client.post(f"/edit_grn/{grn_id}", data={
        "supplier": "Locked Supplier",
        "supplier_id": str(sup_id),
        "date": "2026-09-11",
        "time": "18:30",
        "payment_type": "Credit",
        "manual_bill_no": "OVR-LOCK",
        "grn_item_id[]": [str(item_id)],
        "mat_name[]": ["Locked Cement"],
        "qty[]": ["500"],
        "price[]": ["100"],
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "GRN updated successfully" in flash_text(resp)

    with app.app_context():
        grn = db.session.get(GRN, grn_id)
        assert grn.date_posted.strftime("%Y-%m-%d %H:%M") == "2026-09-11 18:30"
        rows = entries_for(grn)
        assert rows, "stock ledger row must survive a header-only edit"
        assert (rows[0].date, rows[0].time) == ("2026-09-11", "18:30:00"), \
            "stock ledger time drifted from the GRN's own manual time"
        stock_after = float(Material.query.filter_by(name="Locked Cement").first().total or 0)
        assert stock_after == stock_before, "a date/time override must never touch stock"


# ---------------------------------------------------------------------------
# The wizard pre-fills the stored stamp on edit (and offers the helper button)
# ---------------------------------------------------------------------------
def test_edit_page_prefills_manual_date_time(client, app):
    login(client)
    sup_id = seed_supplier_material(app)
    add_grn(client, sup_id, "Override Supplier", "Override Cement",
            date="2026-10-05", time="14:45:30", bill="OVR-P1")

    with app.app_context():
        grn_id = grn_by_bill("OVR-P1").id

    html = client.get(f"/edit_grn/{grn_id}").data.decode()
    assert 'id="grnDateInput"' in html and 'value="2026-10-05"' in html
    assert 'id="grnTimeInput"' in html and 'value="14:45:30"' in html, \
        "edit page must show the stored seconds so they are not wiped on save"
    assert 'id="grnUseNowBtn"' in html

    # The "Add New Record" form carries the same helper.
    add_page = client.get("/grn").data.decode()
    assert 'id="grnDateInput"' in add_page
    assert 'id="grnTimeInput"' in add_page
    assert 'id="grnUseNowBtn"' in add_page
    assert "Use current time" in add_page


# ---------------------------------------------------------------------------
# The list view shows the manual time
# ---------------------------------------------------------------------------
def test_grn_list_shows_manual_time(client, app):
    login(client)
    sup_id = seed_supplier_material(app)
    add_grn(client, sup_id, "Override Supplier", "Override Cement",
            date="2026-10-05", time="2:45 PM", bill="OVR-LIST")

    html = client.get("/grn").data.decode()
    assert "05/Oct/2026" in html
    assert "02:45 PM" in html, "GRN list must show the manually entered time"
