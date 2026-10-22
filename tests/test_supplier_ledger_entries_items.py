"""Supplier ledger entries list: the same QTY / RATE / AMOUNT columns as the PDF.

The ledger statement PDF itemises every GRN material line (quantity, rate and
amount per transaction).  The on-screen "Financial transaction ledger" — the
entries list the user reads day to day — now shows those same three columns:

* a GRN entry stacks one line per material in Description / Qty / Rate /
  Amount, line for line, exactly like the statement's continuation lines;
* payments, opening balances and other money-only rows keep the material
  columns blank (``—``) and their amount in Debit or Credit as before;
* the running balance maths is untouched.
"""
from __future__ import annotations

import re
from datetime import datetime

from models import db, Supplier, GRN, GRNItem, SupplierPayment


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def _ledger_rows(html: str) -> list[str]:
    """Every ``<tr>…</tr>`` of the entries list that carries the item columns."""
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html, re.S)
    return [row for row in rows if 'data-label="Qty"' in row]


def _row_with(rows: list[str], needle: str) -> str:
    for row in rows:
        if needle in row:
            return row
    raise AssertionError(f"ledger row containing {needle!r} not found")


def _empty_item_cell(label: str) -> str:
    return rf'data-label="{label}">\s*<span class="text-white-50">—</span>'


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


def test_entries_list_has_quantity_rate_and_amount_columns(app, client):
    supplier_id = item_supplier(app, client)
    resp = client.get(f"/supplier_ledger/{supplier_id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Same column set as the PDF statement, money columns untouched.
    assert ('<th class="text-end">Qty</th><th class="text-end">Rate</th>'
            '<th class="text-end">Amount</th>') in html
    for header in ("Debit", "Credit", "Running balance"):
        assert f">{header}</th>" in html


def test_grn_entry_shows_itemised_qty_rate_and_amount(app, client):
    supplier_id = item_supplier(app, client)
    resp = client.get(f"/supplier_ledger/{supplier_id}")
    html = resp.get_data(as_text=True)
    rows = _ledger_rows(html)
    grn_row = _row_with(rows, "GRN-ITEM-001")
    # Each material line prints its own name, quantity, rate and line amount.
    for needle in ("DETAIL CEMENT", "DETAIL SAND",
                   "12.50", "275.50", "3,443.75",
                   "3.00", "1,000.00", "3,000.00"):
        assert needle in grn_row, f"{needle!r} missing from the itemised GRN entry"
    # The bill itself is still a single credit of 6,443.75.
    assert "6,443.75" in grn_row


def test_payment_entry_keeps_material_columns_empty(app, client):
    supplier_id = item_supplier(app, client)
    resp = client.get(f"/supplier_ledger/{supplier_id}")
    html = resp.get_data(as_text=True)
    pay_row = _row_with(_ledger_rows(html), "PAY-ITEM-001")
    for label in ("Qty", "Rate", "Amount"):
        assert re.search(_empty_item_cell(label), pay_row), (
            f"payment row must keep the {label} column blank")
    # The money still lands in debit only.
    assert "500.00" in pay_row


def test_entries_balances_are_unchanged(app, client):
    supplier_id = item_supplier(app, client)
    resp = client.get(f"/supplier_ledger/{supplier_id}")
    html = resp.get_data(as_text=True)
    rows = _ledger_rows(html)
    # 1,250 opening + 6,443.75 bill - 500 paid = 7,193.75 closing.
    assert "7,193.75" in _row_with(rows, "PAY-ITEM-001")
    assert "7,193.75" in html  # closing-balance card
