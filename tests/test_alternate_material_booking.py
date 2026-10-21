"""Alternate-material booking delivery: the physically supplied material must
drive stock, GRN/FIFO, bill lines and ledgers, while the originally booked
material is only reduced on the booking balance.

Acceptance scenario (reported bug):
  * GRN stock exists for 12mm and 20mm steel.
  * Customer booking: 1,000 kg of 12mm steel.
  * Actual delivery: 1,100 kg of 20mm steel (alternate) —
    1,000 kg against the booking + 100 kg excess.

Expected after saving:
  * Bill shows 20mm steel on both lines (1,000 ALT + 100 excess = 1,100).
  * 12mm booking is fulfilled by the 1,000 kg alternate.
  * No 12mm stock is deducted; exactly 1,100 kg comes out of 20mm stock.
  * Excess 100 kg is billed at the sale rate; no duplicate entries.
"""
from __future__ import annotations

import re
from datetime import timedelta

from app.services.time_money import pk_today
from app.services.sales_core import format_alt_material_label

from models import (
    db,
    Booking,
    BookingAllocation,
    BookingItem,
    Client,
    DirectSale,
    DirectSaleItem,
    Entry,
    GRN,
    GRNAllocation,
    GRNItem,
    Material,
    PendingBill,
)
from conftest import make_csrf_client

MAT_12 = "12mm Steel"
MAT_20 = "20mm Steel"


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def body_text(response):
    return response.get_data(as_text=True)


def flash_text(response):
    msgs = re.findall(r"alert[^>]*>(.{0,400}?)</div>", body_text(response), re.S)
    return " | ".join(re.sub(r"<[^>]+>|\s+", " ", m).strip() for m in msgs)


def grn_date():
    return (pk_today() - timedelta(days=5)).strftime("%Y-%m-%d")


def seed_material(client, app, name, grn_qty, grn_rate):
    r = client.post("/add_material", data={
        "material_name": name, "material_unit": "Kg",
    }, follow_redirects=True)
    assert r.status_code == 200
    r = client.post("/grn", data={
        "action": "add", "supplier": "SUP1",
        "mat_name[]": name, "qty[]": str(grn_qty), "price[]": str(grn_rate),
        "paid_amount": "0", "date": grn_date(),
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        mat = Material.query.filter_by(name=name).first()
        assert mat is not None and float(mat.total or 0) >= grn_qty
        return mat.id


def seed_client(client, app, name="C1", code="CL-1"):
    r = client.post("/add_client", data={
        "name": name, "code": code, "category": "General", "opening_balance": "0",
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        return Client.query.filter_by(code=code).first().id


def seed_booking(client, app, client_code, material, qty, rate):
    r = client.post("/add_booking", data={
        "client_code": client_code,
        "material_name[]": material,
        "qty[]": str(qty),
        "unit_rate[]": str(rate),
        "amount": str(qty * rate),
        "paid_amount": "0",
    }, follow_redirects=True)
    assert b"Booking added successfully" in r.data, flash_text(r)
    with app.app_context():
        return Booking.query.order_by(Booking.id.desc()).first().id


def seed_alt_scenario(client, app, *, qty="1100", rate="250"):
    """GRN 12mm + 20mm, booking of 1000 x 12mm, mixed sale of qty x 20mm ALT."""
    seed_material(client, app, MAT_12, 2000, 200)
    seed_material(client, app, MAT_20, 2000, 210)
    seed_client(client, app)
    seed_booking(client, app, "CL-1", MAT_12, 1000, 240)

    r = client.post("/add_direct_sale", data={
        "client_name": "C1",
        "client_code": "CL-1",
        "driver_name": "D",
        "category": "Mixed Transaction",
        "product_name[]": MAT_12,
        "alternate_material[]": MAT_20,
        "qty[]": qty,
        "unit_rate[]": rate,
        "payment_method": "Cash",
        "paid_amount": "0",
    }, follow_redirects=True)
    assert "Direct sale added successfully" in body_text(r), flash_text(r)
    with app.app_context():
        return DirectSale.query.order_by(DirectSale.id.desc()).first().id


# ---------------------------------------------------------------------------
# Acceptance scenario
# ---------------------------------------------------------------------------
def test_alternate_with_excess_records_delivered_material(app):
    client = make_csrf_client(app)
    login(client)
    sale_id = seed_alt_scenario(client, app)

    with app.app_context():
        sale = db.session.get(DirectSale, sale_id)
        assert sale.category == "Mixed Transaction"
        # Only the 100 kg excess is chargeable (booking was paid at booking time).
        assert abs(float(sale.amount or 0) - 100 * 250) < 0.01

        items = DirectSaleItem.query.filter_by(sale_id=sale.id).all()
        # Every physical line must be the delivered material: 20mm.
        assert all(it.product_name == MAT_20 for it in items), \
            [(it.product_name, it.qty) for it in items]
        booked_lines = [it for it in items if float(it.price_at_time or 0) <= 0]
        excess_lines = [it for it in items if float(it.price_at_time or 0) > 0]
        assert sum(float(it.qty or 0) for it in booked_lines) == 1000
        assert sum(float(it.qty or 0) for it in excess_lines) == 100
        assert all(abs(float(it.price_at_time) - 250) < 0.01 for it in excess_lines)

        # Ledger entries: all OUT rows ship 20mm; the booking row references 12mm.
        out_entries = Entry.query.filter_by(
            nimbus_no="Direct Sale", type="OUT", is_void=False
        ).all()
        assert all((e.material or "") == MAT_20 for e in out_entries), \
            [(e.material, e.qty) for e in out_entries]
        assert sum(float(e.qty or 0) for e in out_entries) == 1100

        alt_entries = [e for e in out_entries if e.is_alternate]
        assert len(alt_entries) == 1
        assert alt_entries[0].booked_material == MAT_12
        assert float(alt_entries[0].qty or 0) == 1000
        assert alt_entries[0].client_category == "Booking Delivery"

        excess_entries = [e for e in out_entries if not e.is_alternate]
        assert sum(float(e.qty or 0) for e in excess_entries) == 100
        assert all(not (e.booked_material or "") for e in excess_entries)
        assert all(e.client_category == "Credit Customer" for e in excess_entries)

        # Stock: no 12mm deducted, exactly 1,100 out of 20mm.
        mat12 = Material.query.filter_by(name=MAT_12).first()
        mat20 = Material.query.filter_by(name=MAT_20).first()
        assert float(mat12.total or 0) == 2000, "12mm stock must stay untouched"
        assert float(mat20.total or 0) == 900, "20mm stock must drop by 1100"

        # Booking balance: the 12mm booking item is fully consumed by the ALT.
        allocs = BookingAllocation.query.filter_by(sale_id=sale.id, is_void=False).all()
        assert sum(float(a.qty or 0) for a in allocs) == 1000
        for a in allocs:
            assert a.booking_item.material_name == MAT_12

        # FIFO cost: the 100 kg excess consumes 20mm GRN lots only.
        grn_allocs = GRNAllocation.query.filter_by(sale_id=sale.id, is_void=False).all()
        assert sum(float(g.qty or 0) for g in grn_allocs) == 100
        for g in grn_allocs:
            assert g.grn_item.mat_name == MAT_20

        # Billing: one pending bill for the 100 kg excess, no duplicates.
        pbs = PendingBill.query.filter_by(
            source_table="direct_sale", source_id=sale.id, is_void=False
        ).all()
        assert len(pbs) == 1
        assert abs(float(pbs[0].amount or 0) - 25000) < 0.01

        # No duplicate sale/delivery rows.
        assert DirectSale.query.count() == 1
        bill_ref = sale.manual_bill_no or sale.auto_bill_no


def test_bill_view_shows_delivered_material_lines(app):
    client = make_csrf_client(app)
    login(client)
    sale_id = seed_alt_scenario(client, app)

    with app.app_context():
        sale = db.session.get(DirectSale, sale_id)
        bill_ref = sale.manual_bill_no or sale.auto_bill_no

    r = client.get(
        f"/view_bill/{bill_ref}?src=direct_sale&src_id={sale_id}",
        follow_redirects=True,
    )
    html = body_text(r)
    line_names = re.findall(
        r'<td class="text-start fw-bold">\s*(.*?)\s*</td>', html, re.S
    )
    assert line_names, "no bill line items rendered"

    alt_label = format_alt_material_label(MAT_12, MAT_20)
    assert alt_label == f"{MAT_20} (ALT for booked {MAT_12})"
    assert alt_label in line_names, line_names
    # The excess line must show plain 20mm; no bill line may be plain 12mm.
    assert MAT_20 in line_names, line_names
    assert MAT_12 not in line_names, \
        f"12mm must never appear as a supplied-material line: {line_names}"


def test_booking_pool_consumed_prevents_second_alternate(app):
    client = make_csrf_client(app)
    login(client)
    seed_alt_scenario(client, app)

    # The 1,000 kg booking is already fulfilled by the alternate; a second
    # alternate delivery must be rejected (no remaining booked quantity).
    r = client.post("/add_direct_sale", data={
        "client_name": "C1",
        "client_code": "CL-1",
        "driver_name": "D",
        "category": "Mixed Transaction",
        "product_name[]": MAT_12,
        "alternate_material[]": MAT_20,
        "qty[]": "50",
        "unit_rate[]": "250",
        "payment_method": "Cash",
        "paid_amount": "0",
    }, follow_redirects=True)
    assert "Direct sale added successfully" not in body_text(r)
    with app.app_context():
        assert DirectSale.query.count() == 1


def test_alternate_stock_validation_checks_delivered_material(app):
    """Stock must be validated against the alternate (delivered) material."""
    client = make_csrf_client(app)
    login(client)
    seed_material(client, app, MAT_12, 2000, 200)
    seed_material(client, app, MAT_20, 500, 210)  # not enough for 1100 of 20mm
    seed_client(client, app)
    seed_booking(client, app, "CL-1", MAT_12, 1000, 240)

    r = client.post("/add_direct_sale", data={
        "client_name": "C1",
        "client_code": "CL-1",
        "driver_name": "D",
        "category": "Mixed Transaction",
        "product_name[]": MAT_12,
        "alternate_material[]": MAT_20,
        "qty[]": "1100",
        "unit_rate[]": "250",
        "payment_method": "Cash",
        "paid_amount": "0",
    }, follow_redirects=True)
    text = flash_text(r)
    assert "Insufficient stock" in text and MAT_20 in text, text
    with app.app_context():
        assert DirectSale.query.count() == 0
        # Nothing moved.
        assert float(Material.query.filter_by(name=MAT_12).first().total) == 2000
        assert float(Material.query.filter_by(name=MAT_20).first().total) == 500


def test_void_and_unvoid_restore_delivered_material_stock(app):
    client = make_csrf_client(app)
    login(client)
    sale_id = seed_alt_scenario(client, app)

    from app.services.void_rebuild import (
        _void_direct_sale_entries_and_restore_stock,
        _unvoid_direct_sale_entries_and_apply_stock,
    )

    with app.app_context():
        sale = db.session.get(DirectSale, sale_id)

        _void_direct_sale_entries_and_restore_stock(sale)
        db.session.flush()
        # Restoring must put the 1,100 back on 20mm — never touch 12mm.
        assert float(Material.query.filter_by(name=MAT_12).first().total) == 2000
        assert float(Material.query.filter_by(name=MAT_20).first().total) == 2000

        _unvoid_direct_sale_entries_and_apply_stock(sale)
        db.session.flush()
        assert float(Material.query.filter_by(name=MAT_12).first().total) == 2000
        assert float(Material.query.filter_by(name=MAT_20).first().total) == 900
        db.session.rollback()


def test_edit_preserves_delivered_material_mapping(app):
    client = make_csrf_client(app)
    login(client)
    sale_id = seed_alt_scenario(client, app)

    # Re-submit the same sale through the edit route (rate 0 per edit policy:
    # alternates are booked rows; the excess is priced from the delivered
    # material's unit price).
    r = client.post(f"/edit_bill/DirectSale/{sale_id}", data={
        "client_code": "CL-1",
        "client_name": "C1",
        "category": "Mixed Transaction",
        "driver_name": "D",
        "manual_bill_no": "MB-77",
        "product_name[]": MAT_12,
        "alternate_material[]": MAT_20,
        "qty[]": "1100",
        "unit_rate[]": "0",
        "paid_amount": "0",
    }, follow_redirects=True)
    text = body_text(r)
    assert "updated" in text.lower() or "success" in text.lower(), flash_text(r)

    with app.app_context():
        items = DirectSaleItem.query.filter_by(sale_id=sale_id).all()
        assert all(it.product_name == MAT_20 for it in items), \
            [(it.product_name, it.qty) for it in items]
        assert sum(float(it.qty or 0) for it in items) == 1100

        out_entries = Entry.query.filter_by(
            nimbus_no="Direct Sale", type="OUT", is_void=False
        ).all()
        assert all(e.material == MAT_20 for e in out_entries)
        assert sum(float(e.qty or 0) for e in out_entries) == 1100
        alt_rows = [e for e in out_entries if e.is_alternate]
        assert len(alt_rows) == 1 and alt_rows[0].booked_material == MAT_12

        assert float(Material.query.filter_by(name=MAT_12).first().total) == 2000
        assert float(Material.query.filter_by(name=MAT_20).first().total) == 900


def test_normal_booking_delivery_without_alternate_unchanged(app):
    """Regression: plain booked + excess delivery (no alternate) still works."""
    client = make_csrf_client(app)
    login(client)
    seed_material(client, app, "CEMENT", 1000, 100)
    seed_client(client, app)
    seed_booking(client, app, "CL-1", "CEMENT", 500, 120)

    r = client.post("/add_direct_sale", data={
        "client_name": "C1",
        "client_code": "CL-1",
        "driver_name": "D",
        "category": "Mixed Transaction",
        "product_name[]": "CEMENT",
        "qty[]": "600",
        "unit_rate[]": "130",
        "payment_method": "Cash",
        "paid_amount": "0",
    }, follow_redirects=True)
    assert "Direct sale added successfully" in body_text(r), flash_text(r)

    with app.app_context():
        sale = DirectSale.query.order_by(DirectSale.id.desc()).first()
        assert abs(float(sale.amount or 0) - 100 * 130) < 0.01
        items = DirectSaleItem.query.filter_by(sale_id=sale.id).all()
        assert all(it.product_name == "CEMENT" for it in items)
        out_entries = Entry.query.filter_by(
            nimbus_no="Direct Sale", type="OUT", is_void=False
        ).all()
        assert all(e.material == "CEMENT" for e in out_entries)
        assert all(not e.is_alternate and not (e.booked_material or "") for e in out_entries)
        assert sum(float(e.qty or 0) for e in out_entries) == 600
        assert float(Material.query.filter_by(name="CEMENT").first().total) == 400


def test_format_alt_material_label():
    assert format_alt_material_label("12mm Steel", "20mm Steel") == \
        "20mm Steel (ALT for booked 12mm Steel)"
    # Same material => plain name (not an alternate).
    assert format_alt_material_label("CEMENT", "CEMENT") == "CEMENT"
    assert format_alt_material_label(None, "CEMENT") == "CEMENT"
    assert format_alt_material_label("CEMENT", "") == "CEMENT"
